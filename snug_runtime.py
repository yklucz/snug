"""Managed installer support; source/pip installations keep their own environment.

No package manager is installed into Snug. Windows wheels are unpacked directly,
and native packages contribute only the DLLs needed by libarchive and licenses.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from snug_core import ArchiveError

APP = Path(__file__).resolve().parent
FILE_RECORD = ".snug-files.json"


def load_lock(root: Path = APP) -> dict[str, Any]:
    return json.loads((root / "runtime-lock.json").read_text(encoding="utf-8"))


def activate(root: Path = APP, *, read_state: bool = True) -> None:
    """Put app-owned bindings first without modifying a shared Python environment."""
    for path in (root, root / "vendor", Path(os.environ.get("SNUG_PACKAGES", root / "packages"))):
        sys.path.insert(0, str(path))
    library = os.environ.get("SNUG_LIBRARY")
    state = root / "runtime.json"
    if read_state and not library and state.is_file():
        try:
            library = json.loads(state.read_text(encoding="utf-8")).get("library")
        except (OSError, ValueError):
            library = None  # Repair rewrites interrupted or damaged state.
    if not library and (root / "native/libarchive-13.dll").is_file():
        library = "native/libarchive-13.dll"
    if library:
        os.environ["LIBARCHIVE"] = str(root / library) if not Path(library).is_absolute() else library


def recorded_files_exist(directory: Path) -> bool:
    record = directory / FILE_RECORD
    if not record.is_file():
        return False
    try:
        files = json.loads(record.read_text(encoding="utf-8"))
        return isinstance(files, dict) and bool(files) and all((directory / name).is_file() for name in files)
    except (OSError, ValueError):
        return False


def binding_valid(root: Path = APP) -> bool:
    vendor = root / "vendor"
    record = vendor / FILE_RECORD
    if not recorded_files_exist(vendor):
        return False
    files = json.loads(record.read_text(encoding="utf-8"))
    return all(hashlib.sha256((vendor / name).read_bytes()).hexdigest() == sha
               for name, sha in files.items())


def check_backend(backend: str) -> None:
    if backend == "py7zr":
        library = importlib.import_module("py7zr")
        version = tuple(int(part) for part in library.__version__.split(".")[:3])
        if not (1, 1, 3) <= version < (2,):
            raise RuntimeError("py7zr 1.1.3 through 1.x is required")
        if not hasattr(importlib.import_module("py7zr.io"), "WriterFactory"):
            raise RuntimeError("py7zr is missing its streaming writer API")
        # AES loads its native extension lazily. Check it before accepting reuse.
        importlib.import_module("Cryptodome.Cipher.AES").new(bytes(32), 1)
        return
    from snug_ext import LibarchiveBackend
    library = LibarchiveBackend._library()
    if library.ffi.version_number() < 3008000:
        raise RuntimeError("managed installations require libarchive 3.8+")
    required = {"7zip", "ar", "cab", "cpio", "iso9660", "lha", "rar", "xar", "warc", "zip"}
    if not required <= library.ffi.READ_FORMATS:
        raise RuntimeError("libarchive is missing required archive readers")
    if not {"ar_bsd", "cpio_newc"} <= library.ffi.WRITE_FORMATS:
        raise RuntimeError("libarchive is missing required archive writers")


def check(root: Path = APP, backend: str = "all") -> None:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10+ is required")
    if backend in ("all", "libarchive") and not binding_valid(root):
        raise RuntimeError("Snug's libarchive binding needs repair")
    packages = root / "packages"
    if backend in ("all", "py7zr") and packages.exists() and not recorded_files_exist(packages):
        raise RuntimeError("Snug's Python packages need repair")
    native = root / "native"
    if backend in ("all", "libarchive") and native.exists() and not recorded_files_exist(native):
        raise RuntimeError("Snug's native libraries need repair")
    activate(root)
    for name in (("py7zr", "libarchive") if backend == "all" else (backend,)):
        check_backend(name)


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject a redirect downgrade before urllib sends the next request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise RuntimeError("runtime download redirects require HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(artifact: dict[str, Any], target: Path) -> None:
    url = artifact["url"]
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise RuntimeError("runtime downloads require HTTPS")
    opener = urllib.request.build_opener(HTTPSRedirectHandler())
    with opener.open(url, timeout=60) as source, target.open("wb") as output:
        shutil.copyfileobj(source, output, 1024 * 1024)
    if hashlib.sha256(target.read_bytes()).hexdigest() != artifact["sha256"]:
        raise RuntimeError(f"download checksum mismatch: {target.name}")


def member_path(name: str) -> Path:
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or "\\" in name or any(p in ("..", ".") or ":" in p for p in parts):
        raise RuntimeError("unsafe path in runtime package")
    return Path(*parts)


def wheel_member(name: str) -> Path | None:
    path = member_path(name)
    parts = path.parts
    if any(p.lower() in ("tests", "selftest", "__pycache__") for p in parts):
        return None
    if parts[0].endswith(".data"):
        if len(parts) < 3 or parts[1] not in ("purelib", "platlib"):
            return None  # console scripts and build headers are unnecessary
        return Path(*parts[2:])
    return path


def unpack_wheel(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as wheel:
        for entry in wheel.infolist():
            # ZipInfo.filename normalizes backslashes on Windows. Validate
            # the original header name before normalization or directory skips.
            relative = wheel_member(entry.orig_filename)
            if entry.is_dir():
                continue
            if relative is None:
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with wheel.open(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def record_files(directory: Path) -> None:
    files = {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in directory.rglob("*") if p.is_file() and p.name != FILE_RECORD}
    (directory / FILE_RECORD).write_text(json.dumps(files, sort_keys=True), encoding="utf-8")


def replace_directory(staged: Path, destination: Path) -> None:
    backup = destination.with_name(destination.name + ".previous")
    if backup.exists():
        shutil.rmtree(backup)
    if destination.exists():
        destination.rename(backup)
    try:
        staged.rename(destination)
    except OSError:
        if backup.exists():
            backup.rename(destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def ensure_binding(root: Path = APP) -> None:
    if binding_valid(root):
        return
    with tempfile.TemporaryDirectory(prefix=".binding-", dir=root) as temporary:
        work = Path(temporary)
        wheel = work / "binding.whl"
        download(load_lock(root)["binding"], wheel)
        staged = work / "vendor"
        staged.mkdir()
        unpack_wheel(wheel, staged)
        record_files(staged)
        replace_directory(staged, root / "vendor")


def select_wheel(wheels: list[dict[str, Any]], minor: int) -> dict[str, Any]:
    tag = f"cp3{minor}"
    for artifact in wheels:
        python, abi, arch = artifact["filename"].rsplit("-", 3)[1:]
        if arch == "any.whl" and "py3" in python.split("."):
            return artifact
        if arch != "win_amd64.whl":
            continue
        if python == tag and abi == tag:
            return artifact
        if abi == "abi3" and python.startswith("cp3") and int(python[3:]) <= minor:
            return artifact
    raise RuntimeError(f"no locked Windows wheel for Python 3.{minor}")


def install_windows_packages(work: Path, lock: dict[str, Any]) -> Path:
    staged = work / "packages"
    staged.mkdir()
    for name, package in lock["packages"].items():
        if name == "backports.zstd" and sys.version_info >= (3, 14):
            continue
        artifact = select_wheel(package["wheels"], sys.version_info.minor)
        wheel = work / artifact["filename"]
        download(artifact, wheel)
        unpack_wheel(wheel, staged)
        wheel.unlink()
    record_files(staged)
    return staged


def zstd_reader(path: Path):
    module = "compression.zstd" if sys.version_info >= (3, 14) else "backports.zstd"
    return importlib.import_module(module).open(path, "rb")


def unpack_native(archive: Path, destination: Path, package: dict[str, Any]) -> None:
    wanted = set(package["dlls"] + package["licenses"])
    with zstd_reader(archive) as source, tarfile.open(fileobj=source, mode="r|") as entries:
        for entry in entries:
            if entry.name not in wanted:
                continue
            if not entry.isfile():
                raise RuntimeError("native runtime member is not a regular file")
            relative = member_path(entry.name)
            target = destination / (relative.name if entry.name in package["dlls"]
                                    else Path("licenses", *relative.parts[3:]))
            target.parent.mkdir(parents=True, exist_ok=True)
            content = entries.extractfile(entry)
            if content is None:
                raise RuntimeError("native runtime member has no content")
            with content, target.open("wb") as output:
                shutil.copyfileobj(content, output)
            wanted.remove(entry.name)
    if wanted:
        raise RuntimeError(f"native runtime package is incomplete: {package['name']}")


def install_native(work: Path, lock: dict[str, Any]) -> Path:
    staged = work / "native"
    staged.mkdir()
    for package in lock["native"]:
        archive = work / "native.tar.zst"
        download(package, archive)
        unpack_native(archive, staged, package)
        archive.unlink()
    for license_file in lock.get("licenses", []):
        download(license_file, staged / "licenses" / license_file["filename"])
    record_files(staged)
    return staged


def backend_works(name: str, root: Path = APP) -> bool:
    # Loaded DLLs cannot be replaced on Windows. Keep probes in short-lived children.
    completed = subprocess.run([sys.executable, "-B", str(root / "snug_runtime.py"), "--probe", name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return completed.returncode == 0


def windows_repair(root: Path = APP) -> None:
    if sysconfig.get_platform() != "win-amd64" or not (10 <= sys.version_info.minor <= 14):
        raise RuntimeError("managed Windows dependencies require CPython 3.10-3.14 x64")
    ensure_binding(root)
    activate(root)
    lock = load_lock(root)["windows"]
    with tempfile.TemporaryDirectory(prefix=".repair-", dir=root) as temporary:
        work = Path(temporary)
        packages = root / "packages"
        if not backend_works("py7zr", root) or (packages.exists() and not recorded_files_exist(packages)):
            staged = install_windows_packages(work, lock)
            replace_directory(staged, packages)
        # Package imports must start in a fresh process after replacing extensions.
        if packages.exists():
            sys.path.insert(0, str(packages))
        native = root / "native"
        if not backend_works("libarchive", root) or (native.exists() and not recorded_files_exist(native)):
            staged = install_native(work, lock)
            replace_directory(staged, native)
            os.environ["LIBARCHIVE"] = str(native / "libarchive-13.dll")
        library = os.environ.get("LIBARCHIVE", "")
        if library and Path(library).is_relative_to(root):
            library = Path(library).relative_to(root).as_posix()
        state = {"kind": "windows", "python": sys.executable, "library": library}
        (root / "runtime.json").write_text(json.dumps(state), encoding="utf-8")
    completed = subprocess.run([sys.executable, "-B", str(root / "snug_runtime.py"), "--check"], check=False)
    if completed.returncode:
        raise RuntimeError("dependency repair did not pass the startup check")


def storage(root: Path = APP, shared_bytes: int = 0) -> None:
    # Count hardlinks once, and never follow symlinks into shared installations.
    seen: set[tuple[int, int]] = set()
    total = 0
    allocated = 0
    allocation_known = True
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        key = (stat.st_dev, stat.st_ino)
        if key not in seen:
            total += stat.st_size
            allocated += getattr(stat, "st_blocks", 0) * 512
            allocation_known = allocation_known and hasattr(stat, "st_blocks")
            seen.add(key)
    if allocation_known:
        print(f"Snug allocated files: {allocated / 1048576:.2f} MiB; newly added shared Homebrew storage: "
              f"{shared_bytes / 1048576:.2f} MiB; combined: {(allocated + shared_bytes) / 1048576:.2f} MiB.", file=sys.stderr)
    else:
        print(f"Snug files, including managed dependencies: {total / 1048576:.2f} MiB "
              "(file bytes; filesystem allocation may differ).", file=sys.stderr)


def main() -> int:
    command, *args = sys.argv[1:]
    try:
        if command == "--vendor":
            ensure_binding()
        elif command == "--windows-repair":
            windows_repair()
        elif command == "--storage":
            storage(shared_bytes=int(args[0]) if args else 0)
        elif command == "--check":
            check(backend=args[0] if args else "all")
        elif command == "--probe":
            activate()
            check_backend(args[0])
        elif command == "--run":
            if args and args[0] in ("doctor", "formats"):
                activate(read_state=False)
            else:
                check()
            from snug import main as snug_main
            return snug_main(args)
        else:
            raise RuntimeError("unknown runtime command")
    except (OSError, ValueError, RuntimeError, ImportError, AttributeError, ArchiveError) as exc:
        print(f"Snug dependency error: {exc}. Connect to the internet and run snug again to repair.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
