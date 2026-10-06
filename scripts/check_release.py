"""Check source-release completeness and the minimal installed wheel policy."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import sys
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MODULES = {"snug.py", "snug_core.py", "snug_ext.py"}
SOURCE_FILES = {
    *MODULES, "snug_runtime.py", "pyproject.toml", "MANIFEST.in",
    "README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
    ".gitattributes", ".gitignore", "runtime-lock.json",
    "install.sh", "install.ps1", "runtime.sh", "runtime.ps1",
}
LOCAL_DIRS = {
    ".git", ".github", ".venv", "venv", "env", "ENV", "build", "dist",
    "vendor", "packages", "native", "python", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".pyright", ".ruff_cache", ".vscode", ".idea", "htmlcov",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def check_local_paths(names: set[str]) -> None:
    for name in names:
        path = PurePosixPath(name)
        require(not path.is_absolute() and ".." not in path.parts
                and "\\" not in name and not any(":" in part for part in path.parts),
                f"Unsafe release path: {name}")
        require(not (set(path.parts) & LOCAL_DIRS), f"Local directory in release: {name}")
        require(path.name not in {
            "runtime.json", ".snug-files.json", ".coverage", ".DS_Store", "Thumbs.db", ".pypirc",
        },
                f"Generated state in release: {name}")
        require(not path.name.startswith((".env", ".coverage.")),
                f"Local configuration in release: {name}")
        require(path.suffix.lower() not in {
            ".pyc", ".pyo", ".pyd", ".pem", ".key", ".p12", ".pfx", ".part", ".tmp", ".log",
            ".so", ".dylib", ".dll", ".whl",
        }, f"Local file in release: {name}")


def source_files() -> set[str]:
    files = set(SOURCE_FILES)
    for directory in ("docs", "scripts", "tests"):
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                if path.suffix in {".md", ".py"} or "fixtures" in path.parts:
                    files.add(path.relative_to(ROOT).as_posix())
    return files


def check_sdist(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        check_local_paths({member.name for member in members})
        prefixes = {PurePosixPath(member.name).parts[0] for member in members}
        require(len(prefixes) == 1, "Source distribution needs one root directory")
        files = {}
        for member in members:
            require(member.isfile() or member.isdir(), f"Link or special file in source release: {member.name}")
            if member.isfile():
                name = PurePosixPath(*PurePosixPath(member.name).parts[1:]).as_posix()
                require(name not in files, f"Duplicate source-release path: {name}")
                files[name] = member
        check_local_paths(set(files))
        required = source_files()
        require(required <= files.keys(), f"Missing public source files: {sorted(required - files.keys())}")
        for name in sorted(required):
            content = archive.extractfile(files[name])
            require(content is not None, f"Missing source content: {name}")
            if content is not None:
                with content:
                    actual = hashlib.sha256(content.read()).digest()
                expected = hashlib.sha256((ROOT / name).read_bytes()).digest()
                require(actual == expected, f"Source content differs from checkout: {name}")
    print(f"Source release: {len(required)} public files verified; no local/generated files")


def check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
        require(len(names) == len(set(names)), "Duplicate wheel paths")
        check_local_paths(set(names))
        require(MODULES <= set(names), "Wheel is missing a CLI module")
        for name in names:
            require(name in MODULES or (
                len(PurePosixPath(name).parts) > 1
                and PurePosixPath(name).parts[0].endswith(".dist-info")
            ), f"Unnecessary installed wheel file: {name}")
        entry_points = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        require(len(entry_points) == 1, "Wheel needs console-script metadata")
        require("snug = snug:main" in archive.read(entry_points[0]).decode("utf-8"),
                "Wheel has lost the snug console entry point")
    print("Wheel: three CLI modules, package metadata, and the snug entry point verified")


def main() -> int:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist"
    sdists = list(directory.glob("*.tar.gz"))
    wheels = list(directory.glob("*.whl"))
    try:
        require(len(sdists) == len(wheels) == 1, "Build into a clean directory with one sdist and one wheel")
        check_sdist(sdists[0])
        check_wheel(wheels[0])
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"Release check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
