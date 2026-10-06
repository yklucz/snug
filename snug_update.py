"""Small, opt-out update checks and explicit, verified managed-install updates.

Only fixed GitHub release requests leave this module. Archive paths and usage
never enter those requests. Automatic workers are daemon threads: archive work
does not wait for the network, and installation always requires ``snug update``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import total_ordering
import hashlib
import gzip
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any, BinaryIO
import urllib.error
import urllib.parse
import urllib.request
import zlib

if os.name == "nt":
    import msvcrt
else:
    import fcntl

APP = Path(__file__).resolve().parent
API_URL = "https://api.github.com/repos/yklucz/snug/releases/latest"
CHECK_INTERVAL = 24 * 60 * 60
METADATA_LIMIT = 1024 * 1024
STATE_LIMIT = 16 * 1024
DOWNLOAD_LIMIT = 16 * 1024 * 1024
UNPACKED_LIMIT = 64 * 1024 * 1024
CODE_LIMIT = 2 * 1024 * 1024
REQUEST_TIMEOUT = 5
DOWNLOAD_TIMEOUT = 30
INSTALL_MARKER = ".snug-install.json"
OWNED_FILES = ("snug.py", "snug_core.py", "snug_ext.py", "snug_update.py",
               "snug_runtime.py", "runtime.sh", "runtime-lock.json")
_STATE_MUTEX = threading.Lock()
_VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)


class UpdateError(RuntimeError):
    """An actionable update failure suitable for the CLI, without a traceback."""


@total_ordering
@dataclass(frozen=True, eq=False)
class Version:
    """SemVer 2 precedence; build metadata does not affect ordering."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> Version:
        if not isinstance(text, str) or len(text) > 128:
            raise UpdateError("invalid semantic version")
        match = _VERSION.fullmatch(text)
        if not match:
            raise UpdateError("invalid semantic version")
        prerelease = tuple(match[4].split(".")) if match[4] else ()
        if any(part.isdigit() and len(part) > 1 and part[0] == "0" for part in prerelease):
            raise UpdateError("invalid semantic version")
        return cls(int(match[1]), int(match[2]), int(match[3]), prerelease)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return (self.major, self.minor, self.patch, self.prerelease) == (
            other.major, other.minor, other.patch, other.prerelease)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if core != other_core:
            return core < other_core
        if not self.prerelease or not other.prerelease:
            return bool(self.prerelease) and not other.prerelease
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            if left.isdigit() and right.isdigit():
                return int(left) < int(right)
            if left.isdigit() != right.isdigit():
                return left.isdigit()
            return left < right
        return len(self.prerelease) < len(other.prerelease)


@dataclass(frozen=True)
class UpdateResult:
    current_version: str
    latest_version: str
    available: bool
    release: dict[str, Any] | None = None


@dataclass
class AutomaticCheck:
    result: UpdateResult | None = None
    done: threading.Event = field(default_factory=threading.Event)
    notified: bool = False


def state_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Snug/update.json"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        root = Path(local) if local and Path(local).is_absolute() else Path.home() / "AppData/Local"
        return root / "Snug/state/update.json"
    xdg = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local/state"
    return root / "snug/update.json"


def _read_state() -> dict[str, Any]:
    try:
        with state_path().open("rb") as source:
            raw = source.read(STATE_LIMIT + 1)
        if len(raw) > STATE_LIMIT:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        state: dict[str, Any] = {}
        if isinstance(data.get("automatic_checks"), bool):
            state["automatic_checks"] = data["automatic_checks"]
        stamp = data.get("last_check")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and 0 <= stamp <= 1e12:
            state["last_check"] = stamp
        latest = data.get("latest_version")
        if isinstance(latest, str):
            try:
                Version.parse(latest)
                state["latest_version"] = latest
            except UpdateError:
                pass
        return state
    except (OSError, ValueError, UpdateError, RecursionError):
        return {}


def _write_state(state: dict[str, Any]) -> bool:
    """Atomic state writes are best effort, except for explicit preferences."""
    temporary: str | None = None
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".update-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def _state_lock() -> BinaryIO | None:
    """OS-released, nonblocking lock: exiting daemon workers leave no stale owner."""
    path = state_path().with_suffix(".lock")
    lock: BinaryIO | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.open("a+b")
        if os.name == "nt":
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except OSError:
        if lock is not None:
            lock.close()
        return None


def _unlock_state(lock: BinaryIO) -> None:
    try:
        if os.name == "nt":
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        lock.close()


def _record_check(latest: str | None = None) -> None:
    with _STATE_MUTEX:
        lock = _state_lock()
        if lock is None:
            return
        try:
            state = _read_state()
            if latest is None:
                state["last_check"] = time.time()
            else:
                state["latest_version"] = latest
            _write_state(state)
        finally:
            _unlock_state(lock)


def set_checks(enabled: bool) -> None:
    with _STATE_MUTEX:
        lock = _state_lock()
        if lock is None:
            raise UpdateError("unable to save update-check preference; update state is busy or unwritable")
        try:
            state = _read_state()
            state["automatic_checks"] = enabled
            if not _write_state(state):
                raise UpdateError("unable to save update-check preference; update state is unwritable")
        finally:
            _unlock_state(lock)


def _https_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in {
            "api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"
        } and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
                and not any(char.isspace() or char == "\\" for char in url))
    except ValueError:
        return False


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _https_url(newurl):
            raise UpdateError("update redirects require an official GitHub HTTPS host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request(url: str, timeout: int):
    if not _https_url(url):
        raise UpdateError("update requests require an official GitHub HTTPS host")
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json" if url == API_URL else "application/octet-stream",
        "User-Agent": "Snug-update",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return urllib.request.build_opener(HTTPSRedirectHandler()).open(request, timeout=timeout)


def _network_error(exc: BaseException) -> UpdateError:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429):
            return UpdateError("GitHub rate limit reached; try again later")
        if exc.code == 404:
            return UpdateError("no published stable GitHub release is available")
        return UpdateError(f"GitHub returned HTTP {exc.code}")
    if isinstance(exc, TimeoutError) or (isinstance(exc, urllib.error.URLError)
                                       and isinstance(exc.reason, TimeoutError)):
        return UpdateError("network request timed out")
    return UpdateError("network unavailable")


def _fetch_release() -> dict[str, Any]:
    try:
        deadline = time.monotonic() + REQUEST_TIMEOUT
        chunks: list[bytes] = []
        count = 0
        with _request(API_URL, REQUEST_TIMEOUT) as response:
            read = getattr(response, "read1", response.read)
            while True:
                if time.monotonic() > deadline:
                    raise UpdateError("network request timed out")
                chunk = read(min(64 * 1024, METADATA_LIMIT - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > METADATA_LIMIT:
                    raise UpdateError("GitHub release metadata exceeds the size limit")
                chunks.append(chunk)
        raw = b"".join(chunks)
        release = json.loads(raw)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise _network_error(exc) from None
    except (ValueError, UnicodeError, RecursionError):
        raise UpdateError("GitHub returned invalid release JSON") from None
    if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
        raise UpdateError("GitHub returned unexpected release metadata")
    if type(release.get("draft")) is not bool or type(release.get("prerelease")) is not bool:
        raise UpdateError("GitHub returned unexpected release metadata")
    return release


def check_update(current_version: str) -> UpdateResult:
    current = Version.parse(current_version)
    _record_check()
    release = _fetch_release()
    tag = release["tag_name"]
    latest_text = tag[1:] if tag.startswith("v") else tag
    latest = Version.parse(latest_text)
    if release["draft"] or release["prerelease"] or latest.prerelease:
        # The endpoint serves stable releases. Do not promote a mislabeled preview.
        return UpdateResult(current_version, current_version, False)
    _record_check(latest_text)
    return UpdateResult(current_version, latest_text, latest > current, release)


def start_automatic_check(current_version: str) -> AutomaticCheck | None:
    if os.environ.get("SNUG_NO_UPDATE_CHECK"):
        return None
    try:
        Version.parse(current_version)
        with _STATE_MUTEX:
            lock = _state_lock()
            if lock is None:
                return None
            try:
                state = _read_state()
                now = time.time()
                if not state.get("automatic_checks", True):
                    return None
                if 0 <= now - state.get("last_check", 0) < CHECK_INTERVAL:
                    return None
                state["last_check"] = now  # Cache failed attempts, too, before any network.
                if not _write_state(state):
                    return None
            finally:
                _unlock_state(lock)
        handle = AutomaticCheck()

        def worker() -> None:
            try:
                handle.result = check_update(current_version)
            except Exception:
                # Optional discovery must never change an archive command's result.
                pass
            finally:
                handle.done.set()

        threading.Thread(target=worker, name="snug-update-check", daemon=True).start()
        return handle
    except (OSError, UpdateError, RuntimeError):
        return None


def automatic_notice(handle: AutomaticCheck | None) -> str | None:
    """Poll once without waiting. The CLI decides whether stderr is appropriate."""
    if handle is None or not handle.done.is_set() or handle.notified:
        return None
    handle.notified = True
    if os.environ.get("SNUG_NO_UPDATE_CHECK") or not _read_state().get("automatic_checks", True):
        return None
    if handle.result is None or not handle.result.available:
        return None
    return f"Update available: Snug {handle.result.latest_version}\nRun `snug update` to install."


def _managed_root() -> Path:
    guidance = "This Snug installation is managed externally. Please update it using the installation method that installed it."
    if sys.platform == "win32":
        raise UpdateError("Windows managed updates are maintained on the windows branch. " + guidance)
    try:
        root = APP.resolve()
        configured = os.environ.get("SNUG_MANAGED_ROOT")
        if not configured or Path(configured).resolve() != root:
            raise UpdateError(guidance)
    except (OSError, ValueError):
        raise UpdateError("unable to identify a managed Snug installation. " + guidance) from None
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise UpdateError("Source checkouts must be updated with Git. " + guidance)
    if list(root.glob("*.egg-info")) or list(root.glob("*.dist-info")):
        raise UpdateError("Pip installations must be updated with pip. " + guidance)
    marker = root / INSTALL_MARKER
    try:
        if marker.is_symlink() or marker.stat().st_size > STATE_LIMIT:
            raise UpdateError(guidance)
        installation = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise UpdateError(guidance) from None
    if (not isinstance(installation, dict) or type(installation.get("schema")) is not int or installation.get("schema") != 1
            or installation.get("kind") != "homebrew" or installation.get("branch") != "main"):
        raise UpdateError(guidance)
    if (root / ".repair-lock").exists():
        raise UpdateError("A Snug dependency repair is in progress; try updating again when it finishes")
    return root


def _release_asset(result: UpdateResult) -> tuple[str, str, int]:
    release = result.release
    version = result.latest_version
    tag = "v" + version
    name = f"snug_archives-{version}.tar.gz"
    if release is None or release.get("tag_name") != tag:
        raise UpdateError("the stable release must use a vSEMVER tag for managed updates")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("the release has no verified source-distribution asset")
    matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(matching) != 1:
        raise UpdateError(f"the release must provide exactly one {name} asset")
    asset = matching[0]
    digest = asset.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise UpdateError("the release asset is missing its GitHub SHA-256 digest")
    expected = ("https://github.com/yklucz/snug/releases/download/"
                + urllib.parse.quote(tag, safe="") + "/" + urllib.parse.quote(name, safe=""))
    if asset.get("browser_download_url") != expected or asset.get("state") != "uploaded":
        raise UpdateError("the release asset must be an uploaded official Snug HTTPS download")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= DOWNLOAD_LIMIT:
        raise UpdateError("the release asset has an invalid or excessive download size")
    return expected, digest[7:].lower(), size


def _download(url: str, digest: str, size: int, destination: Path) -> None:
    checksum = hashlib.sha256()
    count = 0
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    try:
        with _request(url, DOWNLOAD_TIMEOUT) as response, destination.open("wb") as output:
            read = getattr(response, "read1", response.read)
            while True:
                if time.monotonic() > deadline:
                    raise UpdateError("release download timed out")
                chunk = read(min(64 * 1024, size - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > size or count > DOWNLOAD_LIMIT:
                    raise UpdateError("release download exceeds the expected size")
                checksum.update(chunk)
                output.write(chunk)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise _network_error(exc) from None
    if count != size:
        raise UpdateError("release download is incomplete")
    if checksum.hexdigest() != digest:
        raise UpdateError("release download checksum mismatch")


def _release_files(archive: Path, version: str) -> dict[str, bytes]:
    """Validate the entire sdist, then retain only explicit installer-owned files."""
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0
    root = f"snug_archives-{version}"
    try:
        # Bound decompression itself, including PAX/long-name headers which
        # tarfile handles before yielding a member for our per-file checks.
        with gzip.open(archive, "rb") as compressed:
            raw = compressed.read(UNPACKED_LIMIT + 1)
        if len(raw) > UNPACKED_LIMIT:
            raise UpdateError("release archive exceeds the unpacked size limit")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as source:
            for index, member in enumerate(source):
                if index >= 2048:
                    raise UpdateError("release archive contains too many members")
                name = member.name.rstrip("/") if member.isdir() else member.name
                parts = name.split("/")
                if (not name or "\\" in name or any(not part or part in (".", "..")
                        or ":" in part or "\x00" in part for part in parts)
                        or parts[0] != root or len(name) > 1024):
                    raise UpdateError("release archive contains an unsafe path")
                key = name.casefold()
                if key in seen:
                    raise UpdateError("release archive contains duplicate paths")
                seen.add(key)
                if not (member.isfile() or member.isdir()) or (len(parts) == 1 and not member.isdir()):
                    raise UpdateError("release archive contains a link or unsupported member")
                if member.size < 0 or member.size > DOWNLOAD_LIMIT:
                    raise UpdateError("release archive member exceeds the size limit")
                total += member.size
                if total > UNPACKED_LIMIT:
                    raise UpdateError("release archive exceeds the unpacked size limit")
                if len(parts) != 2 or parts[1] not in OWNED_FILES:
                    continue
                if not member.isfile() or member.size > CODE_LIMIT:
                    raise UpdateError("release application files must be bounded regular files")
                content = source.extractfile(member)
                if content is None:
                    raise UpdateError("release application file is unreadable")
                with content:
                    data = content.read(CODE_LIMIT + 1)
                if len(data) != member.size:
                    raise UpdateError("release application file is incomplete")
                files[parts[1]] = data
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        raise UpdateError("release archive is invalid or unreadable") from None
    if set(files) != set(OWNED_FILES):
        raise UpdateError("release archive is missing required application files")
    return files


def _stage(root: Path, destination: Path, files: dict[str, bytes], version: str) -> None:
    # Keep runtime.json/vendor/packages/native intact, including symlinks without
    # following them into shared Homebrew directories. Only direct code files change.
    shutil.copytree(root, destination, symlinks=True)
    for name, content in files.items():
        target = destination / name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            raise UpdateError("an installed application file is unexpectedly a directory")
        target.write_bytes(content)
        target.chmod(0o755 if name == "runtime.sh" else 0o644)
    marker = destination / INSTALL_MARKER
    marker.unlink(missing_ok=True)
    marker.write_text(json.dumps({"schema": 1, "kind": "homebrew", "branch": "main",
                                  "version": version}) + "\n", encoding="utf-8")


def _validate(root: Path, version: str) -> None:
    environment = os.environ.copy()
    environment.update(SNUG_NO_UPDATE_CHECK="1", PYTHONDONTWRITEBYTECODE="1", SNUG_MANAGED_ROOT=str(root))
    environment.pop("PYTHONPATH", None)
    # When app-owned dependencies exist, probe the staged copies, never repair.
    if (root / "packages").exists():
        environment["SNUG_PACKAGES"] = str(root / "packages")
    try:
        cli = subprocess.run([sys.executable, "-B", str(root / "snug.py"), "--version"],
                             cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=30, check=False)
        if cli.returncode or cli.stdout.strip() != f"snug {version}":
            raise UpdateError("new Snug CLI did not pass version validation")
        backend = subprocess.run([sys.executable, "-B", str(root / "snug_runtime.py"), "--check", "all"],
                                 cwd=root, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=30, check=False)
        if backend.returncode:
            raise UpdateError("new Snug runtime did not pass its offline startup check")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise UpdateError("new Snug installation could not complete offline validation") from None


def perform_update(current_version: str) -> str:
    root = _managed_root()  # Refuse external installations before network access.
    lock = root.parent / f".{root.name}.update-lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise UpdateError("Another Snug update is in progress; try again when it finishes") from None
    except OSError:
        raise UpdateError("Snug's installation directory is not writable") from None
    work: Path | None = None
    keep_backup = False
    try:
        result = check_update(current_version)
        if not result.available:
            return f"Snug {current_version} is up to date."
        url, digest, size = _release_asset(result)
        work = Path(tempfile.mkdtemp(prefix=f".{root.name}.update-", dir=root.parent))
        archive = work / "release.tar.gz"
        _download(url, digest, size, archive)
        files = _release_files(archive, result.latest_version)
        staged = work / "staged"
        _stage(root, staged, files, result.latest_version)
        _validate(staged, result.latest_version)
        if (root / ".repair-lock").exists():
            raise UpdateError("A Snug dependency repair is in progress; try updating again when it finishes")
        backup = work / "previous"
        moved = False
        keep_backup = True  # An interruption must never delete a moved installation.
        try:
            os.replace(root, backup)
            moved = True
            os.replace(staged, root)
            _validate(root, result.latest_version)
        except BaseException as exc:
            if moved or backup.exists():
                try:
                    if root.exists():
                        os.replace(root, staged)
                    os.replace(backup, root)
                except BaseException:
                    keep_backup = True
                    raise UpdateError(f"update rollback could not finish; the previous installation is preserved at {backup}. "
                                      "Restore that directory to the original installation path before retrying") from None
            keep_backup = False
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            reason = str(exc) if isinstance(exc, UpdateError) else "installation replacement failed"
            raise UpdateError(f"{reason}; the previous Snug installation was preserved") from None
        keep_backup = False
        return f"Updated Snug from {current_version} to {result.latest_version}."
    except OSError:
        raise UpdateError("unable to stage the update; the previous Snug installation was preserved") from None
    finally:
        if work is not None and not keep_backup:
            shutil.rmtree(work, ignore_errors=True)
        try:
            lock.rmdir()
        except OSError:
            pass
