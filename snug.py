#!/usr/bin/env python3
"""
Snug — create, inspect and extract archives with live speed and ETA.

Supported formats
-----------------
ZIP (.zip), TAR (.tar), TAR+GZIP (.tar.gz, .tgz),
TAR+BZIP2 (.tar.bz2, .tbz2), TAR+XZ (.tar.xz, .txz).

Large-file support
------------------
* All I/O is streamed in 1 MiB chunks — RAM usage is constant regardless
  of the input size.
* ZIP archives use ZIP64 (``allowZip64=True``) so they may exceed 4 GiB.
* TAR archives use PAX format, which supports arbitrarily large files
  and long paths.

Two ways to use it
------------------
1. Interactive full-screen menu — just run ``snug`` with no arguments.
   The menu occupies the entire terminal (alternate screen buffer) and
   restores your original scrollback when you quit.

2. Command line — for scripting and automation::

       snug create backup.tar.gz src/ README.md
       snug extract backup.tar.gz -C restore/
       snug list backup.tar.gz -v
       snug info backup.tar.gz
"""

from __future__ import annotations

import argparse
import io
import math
import os
import queue
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Literal, Protocol, Sequence

__version__ = "1.7.0"

CHUNK_SIZE = 1 << 20      # 1 MiB
_TTY_REFRESH = 0.1        # seconds between progress redraws


# --------------------------------------------------------------------------- #
#  Color support
# --------------------------------------------------------------------------- #

_CSI = "\x1b["
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class _C:
    """ANSI SGR sequences, named by their visual role."""

    RESET = _CSI + "0m"
    BOLD = _CSI + "1m"
    DIM = _CSI + "2m"
    UNDERLINE = _CSI + "4m"
    REVERSE = _CSI + "7m"

    RED = _CSI + "31m"
    GREEN = _CSI + "32m"
    YELLOW = _CSI + "33m"
    BLUE = _CSI + "34m"
    MAGENTA = _CSI + "35m"
    CYAN = _CSI + "36m"
    WHITE = _CSI + "37m"
    GRAY = _CSI + "90m"

    BOLD_RED = _CSI + "1;31m"
    BOLD_GREEN = _CSI + "1;32m"
    BOLD_YELLOW = _CSI + "1;33m"
    BOLD_BLUE = _CSI + "1;34m"
    BOLD_CYAN = _CSI + "1;36m"
    BOLD_WHITE = _CSI + "1;37m"


_COLOR_ENABLED = False


def _supports_color() -> bool:
    """Honour NO_COLOR, TERM=dumb, and non-TTY stdout."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    return sys.stdout.isatty()


def _init_color() -> None:
    global _COLOR_ENABLED
    _COLOR_ENABLED = _supports_color()


def _paint(text: str, *codes: str) -> str:
    """Wrap *text* in the given SGR codes, or return it unchanged."""
    if not _COLOR_ENABLED or not codes:
        return text
    return "".join(codes) + text + _C.RESET


def _visible_len(text: str) -> int:
    """Length of *text* ignoring ANSI SGR sequences."""
    return len(_ANSI_RE.sub("", text))


def _safe(text: str) -> str:
    """Escape terminal control characters in untrusted archive/member names."""
    return _CONTROL_RE.sub(lambda m: f"\\x{ord(m.group(0)):02x}", str(text))


# --------------------------------------------------------------------------- #
#  Formatting helpers
# --------------------------------------------------------------------------- #

def human_bytes(n: float) -> str:
    if n < 0:
        n = 0.0
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def human_time(seconds: float) -> str:
    if math.isnan(seconds) or seconds < 0 or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _truncate(text: str, width: int) -> str:
    """Truncate *text* to *width* visible columns, preserving ANSI codes."""
    if _visible_len(text) <= width:
        return text

    out: list[str] = []
    visible = 0
    i = 0
    while i < len(text):
        m = _ANSI_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        if visible >= width - 1:
            break
        out.append(text[i])
        visible += 1
        i += 1

    # Carry any trailing SGR codes so existing styling is properly closed.
    while i < len(text):
        m = _ANSI_RE.match(text, i)
        if not m:
            break
        out.append(m.group(0))
        i = m.end()

    suffix = _C.RESET if _COLOR_ENABLED else ""
    return "".join(out) + "…" + suffix


def _shorten_path(path: Path, width: int) -> str:
    text = str(path)
    if len(text) <= width:
        return text
    return "…" + text[-(width - 1):]


# --------------------------------------------------------------------------- #
#  Errors
# --------------------------------------------------------------------------- #

class ArchiveError(Exception):
    """Base error for this module."""


class UnsafeArchiveError(ArchiveError):
    """The archive contains a member that would escape the destination."""


class FormatError(ArchiveError):
    """The archive format could not be determined or is unsupported."""


class _QuitInteractive(Exception):
    """Raised when the user chooses to leave the interactive menu."""


# --------------------------------------------------------------------------- #
#  Formats
# --------------------------------------------------------------------------- #

class ArchiveFormat(str, Enum):
    ZIP = "zip"
    TAR = "tar"
    TAR_GZ = "tar.gz"
    TAR_BZ2 = "tar.bz2"
    TAR_XZ = "tar.xz"

    @property
    def is_zip(self) -> bool:
        return self is ArchiveFormat.ZIP


_SUFFIX_MAP = (
    (".tar.gz", ArchiveFormat.TAR_GZ),
    (".tar.bz2", ArchiveFormat.TAR_BZ2),
    (".tar.xz", ArchiveFormat.TAR_XZ),
    (".zip", ArchiveFormat.ZIP),
    (".tgz", ArchiveFormat.TAR_GZ),
    (".tbz2", ArchiveFormat.TAR_BZ2),
    (".tbz", ArchiveFormat.TAR_BZ2),
    (".txz", ArchiveFormat.TAR_XZ),
    (".tar", ArchiveFormat.TAR),
)

_ARCHIVE_SUFFIXES = tuple(suffix for suffix, _ in _SUFFIX_MAP)

_TarWriteMode = Literal["w", "w:gz", "w:bz2", "w:xz"]

_TAR_WRITE_MODE: dict[ArchiveFormat, _TarWriteMode] = {
    ArchiveFormat.TAR: "w",
    ArchiveFormat.TAR_GZ: "w:gz",
    ArchiveFormat.TAR_BZ2: "w:bz2",
    ArchiveFormat.TAR_XZ: "w:xz",
}

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def detect_format(path, *, for_write: bool = False) -> ArchiveFormat:
    path = Path(path)
    name = path.name.lower()
    for suffix, fmt in _SUFFIX_MAP:
        if name.endswith(suffix):
            return fmt
    if not for_write and path.is_file():
        fmt = _detect_by_magic(path)
        if fmt is not None:
            return fmt
    hint = " (use -f/--format to specify one)" if for_write else ""
    raise FormatError(f"cannot determine archive format for {path.name!r}{hint}")


def _detect_by_magic(path: Path) -> ArchiveFormat | None:
    try:
        with path.open("rb") as fh:
            head = fh.read(8)
            if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
                return ArchiveFormat.ZIP
            if head[:2] == b"\x1f\x8b":
                return ArchiveFormat.TAR_GZ
            if head[:3] == b"BZh":
                return ArchiveFormat.TAR_BZ2
            if head[:6] == b"\xfd7zXZ\x00":
                return ArchiveFormat.TAR_XZ
            fh.seek(257)
            if fh.read(5) == b"ustar":
                return ArchiveFormat.TAR
    except OSError:
        return None
    return None


def _normalize_format(fmt) -> ArchiveFormat:
    if isinstance(fmt, ArchiveFormat):
        return fmt
    return ArchiveFormat(str(fmt).lower().lstrip("."))


# --------------------------------------------------------------------------- #
#  Progress reporting
# --------------------------------------------------------------------------- #

class ProgressSink(Protocol):
    """Structural type satisfied by NullProgress and ProgressDisplay."""

    def start(self, total_bytes: int, total_items: int) -> None: ...
    def item(self, name: str) -> None: ...
    def chunk(self, n: int) -> None: ...
    def done(self) -> None: ...


class NullProgress:
    """No-op progress sink (used when the caller doesn't supply one)."""

    def start(self, total_bytes: int, total_items: int) -> None: ...
    def item(self, name: str) -> None: ...
    def chunk(self, n: int) -> None: ...
    def done(self) -> None: ...


class ProgressDisplay:
    """Renders a single-line progress bar with speed and ETA."""

    BAR_WIDTH = 24

    def __init__(self, operation: str, quiet: bool = False, stream=None) -> None:
        self.operation = operation
        self.quiet = quiet
        self.stream = stream if stream is not None else sys.stderr
        self.tty = self.stream.isatty()
        self.total_bytes = 0
        self.total_items = 0
        self.bytes_done = 0
        self.items_done = 0
        self.current = ""
        self.start_time = time.monotonic()
        self._last_render = 0.0
        self._last_len = 0
        self._lock = threading.Lock()

    def start(self, total_bytes: int, total_items: int) -> None:
        self.total_bytes = max(int(total_bytes), 0)
        self.total_items = max(int(total_items), 0)
        self.start_time = time.monotonic()
        self.bytes_done = 0
        self.items_done = 0
        self._render(force=True)

    def item(self, name: str) -> None:
        with self._lock:
            self.items_done += 1
            self.current = _safe(name)
        self._render()

    def chunk(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self.bytes_done += n
        self._render()

    def done(self) -> None:
        with self._lock:
            self.bytes_done = self.total_bytes
            self.items_done = self.total_items
        self._render(force=True, final=True)

    # -- internals -------------------------------------------------------- #

    def _fraction(self) -> float:
        if self.total_bytes > 0:
            return min(self.bytes_done / self.total_bytes, 1.0)
        if self.total_items > 0:
            return min(self.items_done / self.total_items, 1.0)
        return 1.0

    def _render(self, force: bool = False, final: bool = False) -> None:
        if self.quiet:
            return
        now = time.monotonic()
        if not force and (now - self._last_render) < _TTY_REFRESH:
            return
        self._last_render = now

        elapsed = max(now - self.start_time, 1e-9)
        speed = self.bytes_done / elapsed

        if not self.tty:
            self._render_plain(final, elapsed, speed)
            return

        frac = self._fraction()
        remaining = max(self.total_bytes - self.bytes_done, 0)
        eta = remaining / speed if speed > 0 else float("inf")

        filled = int(frac * self.BAR_WIDTH)
        if _COLOR_ENABLED:
            bar = (
                _C.GREEN + "█" * filled
                + _C.GRAY + "░" * (self.BAR_WIDTH - filled)
                + _C.RESET
            )
        else:
            bar = "█" * filled + "░" * (self.BAR_WIDTH - filled)

        line = (
            f"{_paint(f'{self.operation:>7}', _C.BOLD_CYAN)} |{bar}| "
            f"{frac * 100:5.1f}% "
            f"| {human_bytes(self.bytes_done):>9}/{human_bytes(self.total_bytes):<9} "
            f"| {human_bytes(speed):>9}/s "
            f"| ETA {human_time(eta)} "
            f"| {self.current}"
        )
        cols = shutil.get_terminal_size((120, 24)).columns
        visible = _visible_len(line)
        if visible > cols - 1:
            line = _truncate(line, cols - 1)
            visible = _visible_len(line)
        pad = max(0, self._last_len - visible)
        self.stream.write("\r" + line + " " * pad)
        if final:
            self.stream.write("\n")
        self.stream.flush()
        self._last_len = visible

    def _render_plain(self, final: bool, elapsed: float, speed: float) -> None:
        if not final:
            return
        self.stream.write(
            f"{self.operation}: {self.items_done}/{self.total_items} items, "
            f"{human_bytes(self.bytes_done)} in {human_time(elapsed)} "
            f"(avg {human_bytes(speed)}/s)\n"
        )
        self.stream.flush()


class _ProgressReader(io.RawIOBase):
    """Wraps a binary file object and reports every chunk it yields."""

    def __init__(self, fh, callback: Callable[[int], None]) -> None:
        super().__init__()
        self._fh = fh
        self._cb = callback

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        data = self._fh.read(size)
        if data:
            self._cb(len(data))
        return data


# --------------------------------------------------------------------------- #
#  Path-safety helpers
# --------------------------------------------------------------------------- #

def _clean_parts(name: str) -> list[str]:
    """Split an archive member name into safe components."""
    if not name:
        return []
    name = name.replace("\\", "/")
    parts: list[str] = []
    for raw in name.split("/"):
        if raw in ("", "."):
            continue
        if raw == "..":
            raise UnsafeArchiveError(f"path traversal in member: {name!r}")
        parts.append(raw)
    if parts and _DRIVE_RE.match(parts[0]):
        raise UnsafeArchiveError(f"absolute drive path in member: {name!r}")
    if os.name == "nt":
        for part in parts:
            if ":" in part:
                raise UnsafeArchiveError(
                    f"alternate data stream in member: {name!r}"
                )
    return parts


def _resolve_member(dest: Path, name: str, strip: int) -> Path | None:
    parts = _clean_parts(name)
    if strip > 0:
        parts = parts[strip:]
    if not parts:
        return None
    target = dest.joinpath(*parts)
    try:
        Path(os.path.normpath(target)).relative_to(dest)
    except ValueError:
        raise UnsafeArchiveError(f"member escapes destination: {name!r}") from None
    return target


def _prepare_target(dest_real: Path, target: Path) -> None:
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveError(f"cannot create directory {parent}: {exc}") from exc
    real_parent = Path(os.path.realpath(parent))
    try:
        real_parent.relative_to(dest_real)
    except ValueError:
        raise UnsafeArchiveError(
            f"refusing to write outside destination through a symlink: {target}"
        ) from None


def _make_symlink(
    dest_real: Path, target: Path, link_target: str, overwrite: bool = True
) -> bool:
    """Create a symlink only when its resolved target stays inside dest_real."""
    normalized = link_target.replace("\\", "/")
    if PurePosixPath(normalized).is_absolute() or _DRIVE_RE.match(normalized):
        raise UnsafeArchiveError(
            f"refusing to create absolute symlink "
            f"{target.name!r} -> {link_target!r}"
        )

    resolved = Path(
        os.path.realpath(
            os.path.join(os.path.realpath(target.parent), normalized)
        )
    )
    try:
        resolved.relative_to(dest_real)
    except ValueError:
        raise UnsafeArchiveError(
            f"refusing to create escaping symlink "
            f"{target.name!r} -> {link_target!r}"
        ) from None

    if not _prepare_file_target(target, overwrite):
        return False
    os.symlink(link_target, target)
    return True


def _apply_metadata(path: Path, mode: int | None, mtime: float | None,
                    enabled: bool) -> None:
    if not enabled:
        return
    try:
        if mode:
            os.chmod(path, mode & 0o777)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
    except OSError:
        pass


def _selected(name: str, members) -> bool:
    if members is None:
        return True
    if callable(members):
        return bool(members(name))
    return name in members


def _prepare_file_target(target: Path, overwrite: bool) -> bool:
    """Clear any existing entry at *target*.

    Returns True if the caller may proceed to write, False if the entry
    must be skipped because overwrite is off.
    """
    if target.is_dir() and not target.is_symlink():
        if not overwrite:
            return False
        shutil.rmtree(target)
    elif target.is_symlink() or target.exists():
        if not overwrite:
            return False
        target.unlink()
    return True


def _zip_mtime(info: zipfile.ZipInfo) -> float | None:
    try:
        year, month, day, hour, minute, second = info.date_time
        return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))
    except (ValueError, OverflowError, OSError):
        return None


def _zip_datetime(ts: float) -> tuple[int, int, int, int, int, int]:
    lt = time.localtime(ts)
    return (
        max(lt.tm_year, 1980),
        max(lt.tm_mon, 1),
        max(lt.tm_mday, 1),
        lt.tm_hour,
        lt.tm_min,
        min(lt.tm_sec, 59),
    )


# --------------------------------------------------------------------------- #
#  Directory walking
# --------------------------------------------------------------------------- #

def _join_arc(arcname: str, name: str) -> str:
    """Join a parent arcname and a child name with a forward slash."""
    return f"{arcname}/{name}" if arcname else name


def _walk(path: Path, arcname: str, *, symlinks: str, seen: set[str]):
    """Yield (filesystem_path, archive_name) pairs for *path* and children."""
    if _is_symlink_short_circuit(path, symlinks):
        if symlinks == "store":
            yield path, arcname
        return

    if path.is_dir():
        yield from _walk_dir(path, arcname, symlinks=symlinks, seen=seen)
    else:
        yield path, arcname


def _is_symlink_short_circuit(path: Path, symlinks: str) -> bool:
    """True when the current entry should be yielded or skipped directly
    rather than descended into."""
    return path.is_symlink() and symlinks in ("skip", "store")


def _walk_dir(path: Path, arcname: str, *, symlinks: str, seen: set[str]):
    """Recursive directory traversal; cycle-safe via *seen*."""
    real = os.path.realpath(path)
    if real in seen:
        return
    seen.add(real)
    try:
        yield path, arcname
        children = sorted(path.iterdir(), key=lambda p: p.name)
    except OSError:
        seen.discard(real)
        return
    for child in children:
        yield from _walk(
            child, _join_arc(arcname, child.name),
            symlinks=symlinks, seen=seen,
        )
    seen.discard(real)


# --------------------------------------------------------------------------- #
#  Item collection for archive creation
# --------------------------------------------------------------------------- #

def _require_source(src: Path) -> None:
    if not src.exists() and not src.is_symlink():
        raise ArchiveError(f"no such file or directory: {src}")


def _arcname_for(src: Path, root_abs: Path | None) -> str:
    absolute = Path(os.path.abspath(src))
    if root_abs is None:
        return absolute.name
    try:
        rel = absolute.relative_to(Path(os.path.abspath(root_abs))).as_posix()
    except ValueError as exc:
        raise ArchiveError(
            f"{src} is not inside root {root_abs}"
        ) from exc
    return "" if rel == "." else rel


def _already_archived(fs_path: Path, name: str, archive_real: str,
                      seen_names: set[str]) -> bool:
    """True when the path is the archive itself or a duplicate entry."""
    if os.path.realpath(fs_path) == archive_real:
        return True
    return name in seen_names


def _file_size(fs_path: Path) -> int:
    """Best-effort size of a regular file; 0 for dirs, symlinks and errors."""
    if fs_path.is_symlink() or not fs_path.is_file():
        return 0
    try:
        return fs_path.stat().st_size
    except OSError:
        return 0


def _iter_items(sources, *, root, symlinks: str, archive_real: str, cancel=None):
    """Yield unique archive items, optionally stopping when *cancel* is set."""
    root_abs = Path(os.path.abspath(root)) if root is not None else None
    seen_names: set[str] = set()

    for source in sources:
        if cancel is not None and cancel.is_set():
            return
        src = Path(source)
        _require_source(src)
        arcname = _arcname_for(src, root_abs)

        for fs_path, name in _walk(src, arcname, symlinks=symlinks, seen=set()):
            if cancel is not None and cancel.is_set():
                return
            if not name or _already_archived(fs_path, name, archive_real, seen_names):
                continue
            seen_names.add(name)
            yield fs_path, name


# --------------------------------------------------------------------------- #
#  Reports
# --------------------------------------------------------------------------- #

@dataclass
class CreateReport:
    archive: Path
    format: ArchiveFormat
    entries: int = 0
    files: int = 0
    directories: int = 0
    symlinks: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    elapsed: float = 0.0
    skipped: list[str] = field(default_factory=list)

    @property
    def avg_speed(self) -> float:
        return self.bytes_in / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def ratio(self) -> float:
        return self.bytes_out / self.bytes_in if self.bytes_in else 0.0


@dataclass
class ExtractReport:
    archive: Path
    destination: Path
    files: int = 0
    directories: int = 0
    symlinks: int = 0
    bytes_written: int = 0
    elapsed: float = 0.0
    skipped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.files + self.directories + self.symlinks

    @property
    def avg_speed(self) -> float:
        return self.bytes_written / self.elapsed if self.elapsed > 0 else 0.0


@dataclass(frozen=True)
class ArchiveEntry:
    name: str
    size: int = 0
    compressed_size: int | None = None
    is_dir: bool = False
    is_symlink: bool = False
    link_target: str | None = None
    mode: int | None = None
    mtime: float | None = None


# --------------------------------------------------------------------------- #
#  Extraction context (keeps helper signatures short)
# --------------------------------------------------------------------------- #

@dataclass
class _ExtractContext:
    dest_real: Path
    members: object
    overwrite: bool
    preserve_metadata: bool
    symlinks: str
    strip: int
    report: ExtractReport
    progress: ProgressSink
    deferred_dirs: list[tuple[Path, int | None, float | None]] = field(default_factory=list)


def _tar_compress_kwargs(fmt: ArchiveFormat, level: int | None) -> dict:
    if level is None or fmt is ArchiveFormat.TAR:
        return {}
    if fmt is ArchiveFormat.TAR_XZ:
        return {"preset": max(0, min(level, 9))}
    if fmt is ArchiveFormat.TAR_BZ2:
        return {"compresslevel": max(1, min(level, 9))}
    return {"compresslevel": max(0, min(level, 9))}


# --------------------------------------------------------------------------- #
#  Engine
# --------------------------------------------------------------------------- #

class ArchiveEngine:
    """Create, inspect and extract archives."""

    # ------------------------------------------------------------------ #
    #  Inspection
    # ------------------------------------------------------------------ #

    def list_entries(self, archive) -> list[ArchiveEntry]:
        path = Path(archive)
        if not path.is_file():
            raise ArchiveError(f"no such archive: {path}")
        fmt = detect_format(path)
        if fmt.is_zip:
            with zipfile.ZipFile(path) as zf:
                return [_zip_entry(i) for i in zf.infolist()]
        with tarfile.open(path, "r:*") as tf:
            return [_tar_entry(m) for m in tf.getmembers()]

    def info(self, archive) -> dict:
        path = Path(archive)
        entries = self.list_entries(path)
        return {
            "path": str(path),
            "format": detect_format(path).value,
            "archive_size": path.stat().st_size,
            "entries": len(entries),
            "files": sum(1 for e in entries if not e.is_dir and not e.is_symlink),
            "directories": sum(1 for e in entries if e.is_dir),
            "symlinks": sum(1 for e in entries if e.is_symlink),
            "uncompressed_size": sum(e.size for e in entries if not e.is_dir),
        }

    # ------------------------------------------------------------------ #
    #  Creation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_format(fmt, archive_path: Path, *, for_write: bool) -> ArchiveFormat:
        if fmt is None:
            return detect_format(archive_path, for_write=for_write)
        return _normalize_format(fmt)

    def _dispatch_write(self, archive_path: Path, fmt: ArchiveFormat, items,
                        compresslevel, symlinks, report: CreateReport,
                        progress: ProgressSink) -> None:
        if fmt.is_zip:
            self._write_zip(archive_path, items, compresslevel,
                            symlinks, report, progress)
        else:
            self._write_tar(archive_path, items, fmt, compresslevel,
                            symlinks, report, progress)

    def create(
        self,
        archive,
        sources: Iterable[str | os.PathLike[str]],
        *,
        fmt: ArchiveFormat | str | None = None,
        root: str | os.PathLike[str] | None = None,
        compresslevel: int | None = None,
        symlinks: str = "store",
        progress: ProgressSink | None = None,
        precollected: tuple[list[tuple[Path, str]], int] | None = None,
    ) -> CreateReport:
        if symlinks not in ("store", "follow", "skip"):
            raise ValueError("symlinks must be 'store', 'follow' or 'skip'")
        if compresslevel is not None and not 0 <= compresslevel <= 9:
            raise ValueError("compresslevel must be between 0 and 9")

        progress = progress or NullProgress()
        archive_path = Path(archive)
        fmt = self._resolve_format(fmt, archive_path, for_write=True)

        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_real = os.path.realpath(archive_path)

        if precollected is not None:
            items, total_bytes = precollected
        else:
            items, total_bytes = self._collect_items(
                sources, root=root, symlinks=symlinks, archive_real=archive_real,
            )

        report = CreateReport(archive=archive_path, format=fmt)
        progress.start(total_bytes, len(items))
        start = time.monotonic()

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{archive_path.name}.", suffix=".part",
            dir=str(archive_path.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            self._dispatch_write(tmp_path, fmt, items, compresslevel,
                                 symlinks, report, progress)
            os.replace(tmp_path, archive_path)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        report.elapsed = time.monotonic() - start
        report.entries = len(items)
        report.bytes_out = archive_path.stat().st_size
        progress.done()
        return report

    @staticmethod
    def _collect_items(
        sources, *, root, symlinks: str, archive_real: str
    ) -> tuple[list[tuple[Path, str]], int]:
        """Walk every source and return (items, total_input_bytes)."""
        items: list[tuple[Path, str]] = []
        total_bytes = 0
        for fs_path, name in _iter_items(
            sources, root=root, symlinks=symlinks, archive_real=archive_real
        ):
            items.append((fs_path, name))
            total_bytes += _file_size(fs_path)
        return items, total_bytes

    # -- TAR creation ---------------------------------------------------- #

    def _write_tar(self, archive_path, items, fmt, compresslevel,
                   symlinks, report: CreateReport,
                   progress: ProgressSink) -> None:
        mode: _TarWriteMode = _TAR_WRITE_MODE[fmt]

        kwargs: dict = {
            "format": tarfile.PAX_FORMAT,
            "dereference": symlinks == "follow",
        }
        kwargs.update(_tar_compress_kwargs(fmt, compresslevel))

        with tarfile.open(archive_path, mode=mode, **kwargs) as tf:
            for fs_path, arcname in items:
                progress.item(arcname)
                try:
                    self._add_to_tar(tf, fs_path, arcname, report, progress)
                except (FileNotFoundError, PermissionError):
                    report.skipped.append(arcname)

    @staticmethod
    def _add_to_tar(tf, fs_path: Path, arcname: str,
                    report: CreateReport, progress: ProgressSink) -> None:
        ti = tf.gettarinfo(str(fs_path), arcname=arcname)
        if ti is None:
            report.skipped.append(arcname)
            return

        if ti.issym() or ti.islnk():
            tf.addfile(ti)
            report.symlinks += 1
        elif ti.isdir():
            tf.addfile(ti)
            report.directories += 1
        elif ti.isreg():
            with open(fs_path, "rb") as fh:
                reader = _ProgressReader(fh, progress.chunk)
                tf.addfile(ti, fileobj=reader)
            report.files += 1
            report.bytes_in += ti.size
        else:
            tf.addfile(ti)

    # -- ZIP creation ---------------------------------------------------- #

    def _write_zip(self, archive_path, items, compresslevel,
                   symlinks, report: CreateReport,
                   progress: ProgressSink) -> None:
        kwargs = {}
        if compresslevel is not None:
            kwargs["compresslevel"] = compresslevel

        with zipfile.ZipFile(
            archive_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True, **kwargs
        ) as zf:
            for fs_path, arcname in items:
                progress.item(arcname)
                try:
                    self._add_to_zip(
                        zf, fs_path, arcname, symlinks, report, progress
                    )
                except (FileNotFoundError, PermissionError):
                    report.skipped.append(arcname)

    def _add_to_zip(self, zf, fs_path: Path, arcname: str,
                    symlinks, report: CreateReport,
                    progress: ProgressSink) -> None:
        st = os.stat(fs_path) if symlinks == "follow" else fs_path.lstat()
        if stat.S_ISLNK(st.st_mode):
            self._zip_add_symlink(zf, fs_path, arcname, st, symlinks, report)
        elif fs_path.is_dir():
            self._zip_add_dir(zf, arcname, st, report)
        else:
            self._zip_add_file(zf, fs_path, arcname, st, report, progress)

    @staticmethod
    def _zip_add_symlink(zf, fs_path, arcname, st, symlinks,
                         report: CreateReport) -> None:
        if symlinks == "skip":
            report.skipped.append(arcname)
            return
        zi = zipfile.ZipInfo(arcname, date_time=_zip_datetime(st.st_mtime))
        zi.create_system = 3
        zi.external_attr = (stat.S_IFLNK | 0o777) << 16
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, os.readlink(fs_path))
        report.symlinks += 1

    @staticmethod
    def _zip_add_dir(zf, arcname, st, report: CreateReport) -> None:
        zi = zipfile.ZipInfo(
            arcname.rstrip("/") + "/",
            date_time=_zip_datetime(st.st_mtime),
        )
        zi.create_system = 3
        zi.external_attr = (stat.S_IFDIR | 0o755) << 16
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, b"")
        report.directories += 1

    @staticmethod
    def _zip_add_file(zf, fs_path, arcname, st, report: CreateReport,
                      progress: ProgressSink) -> None:
        zi = zipfile.ZipInfo(arcname, date_time=_zip_datetime(st.st_mtime))
        zi.create_system = 3
        zi.external_attr = (st.st_mode & 0xFFFF) << 16
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.file_size = st.st_size
        with open(fs_path, "rb") as src, zf.open(zi, "w") as dst:
            while True:
                buf = src.read(CHUNK_SIZE)
                if not buf:
                    break
                dst.write(buf)
                progress.chunk(len(buf))
        report.files += 1
        report.bytes_in += st.st_size

    # ------------------------------------------------------------------ #
    #  Extraction
    # ------------------------------------------------------------------ #

    def extract(
        self,
        archive,
        destination=".",
        *,
        members: Sequence[str] | Callable[[str], bool] | None = None,
        overwrite: bool = True,
        preserve_metadata: bool = True,
        symlinks: str = "store",
        strip_components: int = 0,
        progress: ProgressSink | None = None,
    ) -> ExtractReport:
        if symlinks not in ("store", "skip"):
            raise ValueError("symlinks must be 'store' or 'skip'")
        if strip_components < 0:
            raise ValueError("strip_components must be >= 0")

        progress = progress or NullProgress()

        archive_path = Path(archive)
        if not archive_path.is_file():
            raise ArchiveError(f"no such archive: {archive_path}")

        dest = Path(destination)
        dest.mkdir(parents=True, exist_ok=True)
        dest_real = Path(os.path.realpath(dest))

        fmt = detect_format(archive_path)
        report = ExtractReport(archive=archive_path, destination=dest)
        start = time.monotonic()

        member_filter = (
            set(members)
            if isinstance(members, (list, tuple, set, frozenset))
            else members
        )

        ctx = _ExtractContext(
            dest_real=dest_real,
            members=member_filter,
            overwrite=overwrite,
            preserve_metadata=preserve_metadata,
            symlinks=symlinks,
            strip=strip_components,
            report=report,
            progress=progress,
        )

        if fmt.is_zip:
            self._extract_zip(archive_path, ctx)
        else:
            self._extract_tar(archive_path, ctx)

        for path, mode, mtime in sorted(
            ctx.deferred_dirs, key=lambda item: len(item[0].parts), reverse=True
        ):
            _apply_metadata(path, mode, mtime, ctx.preserve_metadata)

        report.elapsed = time.monotonic() - start
        progress.done()
        return report

    # -- ZIP extraction -------------------------------------------------- #

    def _extract_zip(self, archive_path, ctx: _ExtractContext) -> None:
        with zipfile.ZipFile(archive_path) as zf:
            infos = [i for i in zf.infolist() if _selected(i.filename, ctx.members)]
            total_bytes = sum(i.file_size for i in infos if not i.is_dir())
            ctx.progress.start(total_bytes, len(infos))
            for info in infos:
                ctx.progress.item(info.filename)
                self._extract_zip_member(zf, info, ctx)

    def _extract_zip_member(self, zf, info: zipfile.ZipInfo,
                            ctx: _ExtractContext) -> None:
        target = _resolve_member(ctx.dest_real, info.filename, ctx.strip)
        if target is None:
            return

        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            self._extract_zip_symlink(zf, info, target, ctx)
            return
        if info.is_dir() or stat.S_ISDIR(mode):
            self._extract_zip_dir(info, target, mode, ctx)
            return
        self._extract_zip_file(zf, info, target, mode, ctx)

    @staticmethod
    def _extract_zip_symlink(zf, info, target, ctx: _ExtractContext) -> None:
        if ctx.symlinks == "skip":
            ctx.report.skipped.append(info.filename)
            return
        link_target = zf.read(info).decode("utf-8", "surrogateescape")
        _prepare_target(ctx.dest_real, target)
        if _make_symlink(ctx.dest_real, target, link_target, ctx.overwrite):
            ctx.report.symlinks += 1
        else:
            ctx.report.skipped.append(info.filename)

    @staticmethod
    def _extract_zip_dir(info, target, mode, ctx: _ExtractContext) -> None:
        _prepare_target(ctx.dest_real, target)
        target.mkdir(parents=True, exist_ok=True)
        ctx.deferred_dirs.append((target, mode or None, _zip_mtime(info)))
        ctx.report.directories += 1

    @staticmethod
    def _extract_zip_file(zf, info, target, mode, ctx: _ExtractContext) -> None:
        _prepare_target(ctx.dest_real, target)
        if not _prepare_file_target(target, ctx.overwrite):
            ctx.report.skipped.append(info.filename)
            return

        with zf.open(info) as src, open(target, "wb") as dst:
            while True:
                buf = src.read(CHUNK_SIZE)
                if not buf:
                    break
                dst.write(buf)
                ctx.progress.chunk(len(buf))
                ctx.report.bytes_written += len(buf)

        _apply_metadata(target, mode, _zip_mtime(info), ctx.preserve_metadata)
        ctx.report.files += 1

    # -- TAR extraction -------------------------------------------------- #

    def _extract_tar(self, archive_path, ctx: _ExtractContext) -> None:
        with tarfile.open(archive_path, "r:*") as tf:
            members = [m for m in tf.getmembers() if _selected(m.name, ctx.members)]
            total_bytes = sum(m.size for m in members if m.isfile())
            ctx.progress.start(total_bytes, len(members))

            for member in members:
                ctx.progress.item(member.name)
                self._extract_tar_member(tf, member, ctx)

    def _extract_tar_member(self, tf, member: tarfile.TarInfo,
                            ctx: _ExtractContext) -> None:
        target = _resolve_member(ctx.dest_real, member.name, ctx.strip)
        if target is None:
            return

        if member.isdir():
            self._extract_tar_dir(member, target, ctx)
            return
        if member.issym():
            self._extract_tar_symlink(member, target, ctx)
            return
        if member.islnk():
            self._extract_tar_hardlink(member, target, ctx)
            return
        if member.isfile():
            self._extract_tar_file(tf, member, target, ctx)
            return
        ctx.report.skipped.append(member.name)

    @staticmethod
    def _extract_tar_dir(member, target, ctx: _ExtractContext) -> None:
        _prepare_target(ctx.dest_real, target)
        target.mkdir(parents=True, exist_ok=True)
        ctx.deferred_dirs.append((target, member.mode, member.mtime))
        ctx.report.directories += 1

    @staticmethod
    def _extract_tar_symlink(member, target, ctx: _ExtractContext) -> None:
        if ctx.symlinks == "skip":
            ctx.report.skipped.append(member.name)
            return
        _prepare_target(ctx.dest_real, target)
        if _make_symlink(ctx.dest_real, target, member.linkname, ctx.overwrite):
            ctx.report.symlinks += 1
        else:
            ctx.report.skipped.append(member.name)

    @staticmethod
    def _extract_tar_hardlink(member, target, ctx: _ExtractContext) -> None:
        _prepare_target(ctx.dest_real, target)
        link_src = _resolve_member(ctx.dest_real, member.linkname, ctx.strip)
        if link_src is None or not link_src.exists():
            ctx.report.skipped.append(member.name)
            return
        if not _prepare_file_target(target, ctx.overwrite):
            ctx.report.skipped.append(member.name)
            return
        try:
            os.link(link_src, target)
            ctx.report.files += 1
        except OSError:
            ctx.report.skipped.append(member.name)

    @staticmethod
    def _extract_tar_file(tf, member, target, ctx: _ExtractContext) -> None:
        _prepare_target(ctx.dest_real, target)
        if not _prepare_file_target(target, ctx.overwrite):
            ctx.report.skipped.append(member.name)
            return

        src = tf.extractfile(member)
        if src is None:
            ctx.report.skipped.append(member.name)
            return

        with src, open(target, "wb") as dst:
            while True:
                buf = src.read(CHUNK_SIZE)
                if not buf:
                    break
                dst.write(buf)
                ctx.progress.chunk(len(buf))
                ctx.report.bytes_written += len(buf)

        _apply_metadata(target, member.mode, member.mtime, ctx.preserve_metadata)
        ctx.report.files += 1


# --------------------------------------------------------------------------- #
#  Entry conversion
# --------------------------------------------------------------------------- #

def _zip_entry(info: zipfile.ZipInfo) -> ArchiveEntry:
    mode = info.external_attr >> 16
    return ArchiveEntry(
        name=info.filename,
        size=info.file_size,
        compressed_size=info.compress_size,
        is_dir=info.is_dir(),
        is_symlink=stat.S_ISLNK(mode),
        mode=stat.S_IMODE(mode) if mode else None,
        mtime=_zip_mtime(info),
    )


def _tar_entry(member: tarfile.TarInfo) -> ArchiveEntry:
    return ArchiveEntry(
        name=member.name,
        size=member.size,
        is_dir=member.isdir(),
        is_symlink=member.issym(),
        link_target=member.linkname or None,
        mode=stat.S_IMODE(member.mode),
        mtime=float(member.mtime),
    )


# =========================================================================== #
#  Interactive TUI helpers
# =========================================================================== #

# -- ANSI escape sequences -------------------------------------------------- #
_ESC = "\x1b["
_CLEAR_LINE = _ESC + "K"          # clear from cursor to end of line
_CLEAR_BELOW = _ESC + "J"         # clear from cursor to end of screen
_HOME = _ESC + "H"                # cursor to top-left
_HIDE_CURSOR = _ESC + "?25l"
_SHOW_CURSOR = _ESC + "?25h"
_ENTER_ALT_SCREEN = _ESC + "?1049h"
_EXIT_ALT_SCREEN = _ESC + "?1049l"

_NL = "\r\n"                      # raw-mode newline


# -- Screen abstraction ----------------------------------------------------- #

class _ExitMarker:
    """Sentinel returned by ``Screen.handle`` to terminate ``_run_screen``."""

    __slots__ = ()


_EXIT = _ExitMarker()


class Screen(Protocol):
    """A single TUI screen driven by _run_screen."""

    def draw(self, cols: int, rows: int) -> list[str]: ...
    def footer(self) -> list[str]: ...
    def handle(self, key: str) -> "Screen | _ExitMarker | None": ...


def _run_screen(initial: Screen) -> None:
    """Own raw mode, redraw, and key reading for one screen loop.

    A handler returns ``None`` to stay on the current screen, a new
    ``Screen`` to transition, or ``_EXIT`` to terminate the loop.
    """
    screen: Screen = initial
    with _raw_mode():
        while True:
            cols, rows = _term_size()
            _draw_lines(screen.draw(cols, rows), footer=screen.footer())
            key = _read_key()
            nxt = screen.handle(key)
            if nxt is _EXIT:
                return
            if nxt is not None and not isinstance(nxt, _ExitMarker):
                screen = nxt


def _cursor_at(row: int, col: int = 1) -> str:
    return f"{_ESC}{row};{col}H"


def _enable_ansi_windows() -> None:
    """Enable virtual terminal processing on Windows consoles."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _clear_screen() -> None:
    """Home the cursor and wipe everything below."""
    sys.stdout.write(_HOME + _CLEAR_BELOW)
    sys.stdout.flush()


@contextmanager
def _fullscreen():
    """Enter the alternate screen buffer; restore the original on exit."""
    out = sys.stdout
    out.write(_ENTER_ALT_SCREEN + _HIDE_CURSOR + _HOME + _CLEAR_BELOW)
    out.flush()
    try:
        yield
    finally:
        out.write(_SHOW_CURSOR + _EXIT_ALT_SCREEN)
        out.flush()


@contextmanager
def _raw_mode():
    """Put the terminal into raw (unbuffered, no-echo) mode."""
    if os.name == "nt":
        yield
        return

    import termios
    import tty

    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error as exc:
        raise RuntimeError("stdin is not a terminal") from exc

    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# -- key reader ------------------------------------------------------------- #

def _read_key() -> str:
    """Return one of: up, down, left, right, enter, esc, space, backspace,
    quit, or a single printable character."""
    if os.name == "nt":
        return _read_key_windows()
    return _read_key_unix()


# -- Windows ---------------------------------------------------------------- #

def _read_key_windows() -> str:
    import msvcrt

    getch = getattr(msvcrt, "getch")
    ch = getch()
    if ch == b"\x03":
        raise KeyboardInterrupt
    if ch in (b"\xe0", b"\x00"):  # extended key prefix
        return _decode_extended_windows(getch)
    return _decode_simple_char(ch)


def _decode_extended_windows(getch) -> str:
    ch2 = getch()
    if ch2 == b"H":
        return "up"
    if ch2 == b"P":
        return "down"
    if ch2 == b"K":
        return "left"
    if ch2 == b"M":
        return "right"
    return "other"


# -- Unix ------------------------------------------------------------------- #

def _read_key_unix() -> str:
    fd = sys.stdin.fileno()
    b = os.read(fd, 1)
    if not b:
        raise KeyboardInterrupt
    if b == b"\x03":
        raise KeyboardInterrupt
    if b == b"\x1b":
        return _read_arrow_sequence(fd)
    return _decode_simple_char(b)


def _read_arrow_sequence(fd: int) -> str:
    """Decode an ANSI escape sequence and consume it completely."""
    import select

    if not select.select([fd], [], [], 0.05)[0]:
        return "esc"
    introducer = os.read(fd, 1)
    if introducer not in (b"[", b"O"):
        return "esc"

    while True:
        if not select.select([fd], [], [], 0.05)[0]:
            return "other"
        b = os.read(fd, 1)
        if not b:
            raise KeyboardInterrupt
        if 0x40 <= b[0] <= 0x7E:
            return {
                b"A": "up",
                b"B": "down",
                b"C": "right",
                b"D": "left",
            }.get(b, "other")


def _decode_simple_char(b: bytes) -> str:
    """Map a single non-escape byte to a key name or the character itself."""
    if not b:
        raise KeyboardInterrupt
    if b == b"\x1b":
        return "esc"
    if b in (b"\r", b"\n"):
        return "enter"
    if b == b" ":
        return "space"
    if b in (b"\x7f", b"\x08"):
        return "backspace"
    try:
        c = b.decode("ascii")
    except UnicodeDecodeError:
        return "other"
    if not c.isprintable():
        return "other"
    return c


# -- full-screen renderer --------------------------------------------------- #

def _term_size() -> tuple[int, int]:
    size = shutil.get_terminal_size((100, 30))
    return size.columns, size.lines


def _draw_lines(lines: list[str], footer: list[str] | None = None) -> None:
    """Home the cursor, wipe the screen, draw *lines*, then pin *footer* to
    the bottom rows."""
    cols, rows = _term_size()
    out = sys.stdout
    out.write(_HOME + _CLEAR_BELOW)
    for line in lines:
        out.write(_C.RESET + _truncate(line, cols - 1) + _CLEAR_LINE + _NL)
    if footer:
        start_row = rows - len(footer) + 1
        for i, line in enumerate(footer):
            out.write(_cursor_at(start_row + i) + _C.RESET + _CLEAR_LINE
                      + _truncate(line, cols - 1))
    out.flush()


def _draw_footer(lines: list[str]) -> None:
    """Redraw just the footer rows at the bottom of the screen."""
    cols, rows = _term_size()
    out = sys.stdout
    start_row = rows - len(lines) + 1
    for i, line in enumerate(lines):
        out.write(_cursor_at(start_row + i) + _C.RESET + _CLEAR_LINE
                  + _truncate(line, cols - 1))
    out.flush()


# -- generic option menu ---------------------------------------------------- #

def _match_option_key(key: str, options: list[tuple[str, str]]) -> bool:
    return any(key_str == key for key_str, _ in options)


class _MenuScreen:
    """Full-screen option menu."""

    def __init__(self, title: str, subtitle: str | None,
                 options: list[tuple[str, str]]) -> None:
        self.title = title
        self.subtitle = subtitle
        self.options = options
        self.selected = 0
        self.result: str | None = None

    def draw(self, cols: int, rows: int) -> list[str]:
        width = min(cols - 6, 60)
        bar = "═" * width
        header = f"Snug — archive creation & extraction  v{__version__}".center(width)

        lines: list[str] = [
            "",
            "  " + _paint(bar, _C.CYAN),
            "  " + _paint(header, _C.BOLD_CYAN),
            "  " + _paint(bar, _C.CYAN),
            "",
        ]
        if self.subtitle:
            lines.append("  " + _paint(_truncate(self.subtitle, cols - 4), _C.DIM))
            lines.append("")
        lines.append("  " + _paint(self.title, _C.BOLD))
        lines.append("")

        room = max(1, rows - len(lines) - len(self.footer()))
        start, end = _visible_range(len(self.options), self.selected, room)
        for i in range(start, end):
            key, label = self.options[i]
            if i == self.selected:
                lines.append(
                    "  " + _paint("❯", _C.BOLD_GREEN)
                    + _paint(f" {key})  {label}", _C.BOLD_WHITE)
                )
            else:
                lines.append("  " + f"  {key})  {label}")
        return lines

    def footer(self) -> list[str]:
        return [
            "",
            "  " + _paint(
                "↑/↓ move  •  enter select  •  0-4 jump  •  q quit",
                _C.DIM,
            ),
        ]

    def handle(self, key: str) -> "Screen | _ExitMarker | None":
        if key in ("up", "down"):
            step = -1 if key == "up" else 1
            self.selected = (self.selected + step) % len(self.options)
            return None
        if key == "enter":
            self.result = self.options[self.selected][0]
            return _EXIT
        if key in ("esc", "quit", "q", "Q"):
            self.result = None
            return _EXIT
        if key.isdigit() and _match_option_key(key, self.options):
            self.result = key
            return _EXIT
        return None


def _select_menu_plain(title: str, options: list[tuple[str, str]]) -> str | None:
    """Fallback numbered prompt used when stdin/stdout is not a TTY."""
    print(f"  {title}")
    for key, label in options:
        print(f"    {key})  {label}")
    while True:
        try:
            raw = input("  Select: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            continue
        if raw in ("q", "quit", "exit"):
            return None
        if _match_option_key(raw, options):
            return raw
        print(f"  Unknown option {raw!r}.")


def _select_menu(title: str, options: list[tuple[str, str]],
                 subtitle: str | None = None) -> str | None:
    """Display a full-screen arrow-navigable menu."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _select_menu_plain(title, options)
    screen = _MenuScreen(title=title, subtitle=subtitle, options=options)
    _run_screen(screen)
    return screen.result


def _wait_for_enter() -> None:
    """Show a hint at the bottom of the screen and wait for Enter."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    _draw_footer(["", "  " + _paint("Press Enter to continue…", _C.DIM)])
    with _raw_mode():
        while True:
            if _read_key() == "enter":
                return


# =========================================================================== #
#  File picker (arrow-key source selection)
# =========================================================================== #

_PICKER_MIN_ROWS = 5


def _is_enterable(path: Path) -> bool:
    """True for directories we can navigate into (not symlinks to dirs)."""
    return path.is_dir() and not path.is_symlink()


def _list_dir_entries(path: Path) -> list[Path]:
    """Return entries sorted: subdirectories first, then files and symlinks."""
    try:
        items = list(path.iterdir())
    except OSError:
        return []
    dirs = sorted((p for p in items if _is_enterable(p)),
                  key=lambda p: p.name.lower())
    rest = sorted((p for p in items if not _is_enterable(p)),
                  key=lambda p: p.name.lower())
    return dirs + rest


def _visible_range(total: int, cursor: int, rows: int) -> tuple[int, int]:
    if total <= rows:
        return 0, total
    start = cursor - rows // 2
    if start < 0:
        start = 0
    elif start + rows > total:
        start = total - rows
    return start, start + rows


def _entry_name(entry: Path) -> str:
    if _is_enterable(entry):
        return entry.name + "/  ▸"
    if entry.is_symlink():
        return entry.name + "@"
    return entry.name


def _picker_help_line() -> str:
    return ("↑/↓ move  •  → enter dir  •  ← up  •  space mark  •  "
            "enter confirm  •  / filter  •  q cancel")


# -- breadcrumb rendering --------------------------------------------------- #

def _origin_index(current: Path, origin: Path) -> int | None:
    """Index into ``current.parts`` matching the last component of *origin*,
    or ``None`` if *origin* is not a prefix of *current*."""
    origin_parts = tuple(origin.parts)
    current_parts = tuple(current.parts)
    if (len(origin_parts) <= len(current_parts)
            and current_parts[:len(origin_parts)] == origin_parts):
        return len(origin_parts) - 1
    return None


def _labels_width(labels: list[str]) -> int:
    """Visible width of the labels joined by the separator ' › '."""
    return sum(len(s) for s in labels) + 3 * max(0, len(labels) - 1)


def _breadcrumb_indices(current: Path, budget: int) -> list[int | None]:
    """Return indices into ``current.parts``, with ``None`` marking an
    inserted '…'. The first component and the last two are always kept
    when the full path does not fit."""
    parts = list(current.parts)
    if not parts:
        return []
    if _labels_width([str(p) for p in parts]) <= budget or len(parts) <= 3:
        return list(range(len(parts)))
    for keep_tail in range(2, len(parts) - 1):
        candidate: list[int | None] = (
            [0, None] + list(range(len(parts) - keep_tail, len(parts)))
        )
        labels = ["…" if i is None else str(parts[i]) for i in candidate]
        if _labels_width(labels) <= budget:
            return candidate
    return [None]


def _breadcrumb(current: Path, origin: Path, width: int) -> str:
    """Render a breadcrumb with the origin component highlighted."""
    if not current.parts:
        return _paint("⬤", _C.BOLD_CYAN) + " " + _paint("/", _C.BOLD_CYAN)

    sep = " › "
    prefix = "⬤ "
    budget = max(width - len(prefix), 8)

    display_idx = _breadcrumb_indices(current, budget)
    origin_idx = _origin_index(current, origin)
    if origin_idx is not None:
        origin_idx = (
            display_idx.index(origin_idx) if origin_idx in display_idx else None
        )

    labels = ["…" if i is None else current.parts[i] for i in display_idx]
    rendered = [
        _paint(label, _C.BOLD_YELLOW) if i == origin_idx else label
        for i, label in enumerate(labels)
    ]
    return _paint("⬤", _C.BOLD_CYAN) + " " + _paint(sep, _C.DIM).join(rendered)


class _PickerScreen:
    """Full-screen source picker with live filter and breadcrumb."""

    _NAV_KEYS = frozenset(("up", "down", "left", "right"))

    def __init__(self, origin: Path) -> None:
        self.origin = origin
        self.current = origin
        self.entries = _list_dir_entries(origin)
        self.cursor = 0
        self.marked: set[Path] = set()
        self.filter = ""
        self.filter_mode = False
        self.result: list[Path] | None = None
        self._row_count = 12

    # -- helpers -------------------------------------------------------- #

    def _visible(self) -> list[Path]:
        if not self.filter:
            return self.entries
        needle = self.filter.lower()
        return [p for p in self.entries if needle in p.name.lower()]

    def _marked_line(self) -> str:
        if self.marked:
            names = sorted(p.name for p in self.marked)
            text = f"  Marked ({len(self.marked)}): " + ", ".join(names)
            return _paint(text, _C.BOLD_YELLOW)
        return _paint(
            "  Marked: (none — Enter uses the highlighted item)", _C.DIM
        )

    # -- Screen protocol ------------------------------------------------ #

    def draw(self, cols: int, rows: int) -> list[str]:
        self._row_count = max(_PICKER_MIN_ROWS, rows - 8)
        visible = self._visible()
        self._clamp_cursor(len(visible))

        lines = self._header_lines(cols)
        if visible:
            lines.extend(self._render_entries(visible))
        else:
            lines.extend(self._render_empty())
        return lines

    def _clamp_cursor(self, total: int) -> None:
        self.cursor = max(0, min(self.cursor, total - 1)) if total else 0

    def _header_lines(self, cols: int) -> list[str]:
        lines = [
            "",
            "  " + _paint("Select sources to archive", _C.BOLD),
            "  " + _paint("─" * min(cols - 6, 50), _C.CYAN),
            "  " + _breadcrumb(self.current, self.origin, cols - 4),
        ]
        if self.filter:
            lines.append("  " + _paint(f"Filter: {self.filter}", _C.BOLD_YELLOW))
        else:
            lines.append("")
        return lines

    def _render_empty(self) -> list[str]:
        msg = "(no matches)" if self.entries else "(empty directory)"
        return ["  " + _paint(msg, _C.DIM)] + [""] * (self._row_count - 1)

    def _render_entries(self, visible: list[Path]) -> list[str]:
        start, end = _visible_range(len(visible), self.cursor, self._row_count)
        lines = [self._render_entry(i, visible[i]) for i in range(start, end)]
        lines.extend([""] * (self._row_count - (end - start)))
        return lines

    def _render_entry(self, i: int, entry: Path) -> str:
        cursor_marker = (
            _paint("❯", _C.BOLD_GREEN) if i == self.cursor else " "
        )
        mark_marker = (
            _paint("✓", _C.BOLD_YELLOW) if entry in self.marked else " "
        )
        name = _entry_name(entry)
        if _is_enterable(entry):
            name = _paint(name, _C.BOLD_BLUE)
        elif entry.is_symlink():
            name = _paint(name, _C.CYAN)
        return f"  {cursor_marker} {mark_marker} {name}"

    def footer(self) -> list[str]:
        return [
            "  " + _paint(_picker_help_line(), _C.DIM),
            self._marked_line(),
        ]

    def handle(self, key: str) -> "Screen | _ExitMarker | None":
        if key in ("esc", "backspace", "space"):
            return self._handle_filter_or_action(key)
        if key == "enter":
            self._confirm()
            return _EXIT
        if key == "quit":
            self._cancel()
            return _EXIT
        if key in self._NAV_KEYS:
            self._handle_nav(key)
            return None
        if key == "/":
            self.filter_mode = True
            return None
        return self._handle_text(key)

    def _handle_filter_or_action(
        self, key: str
    ) -> "Screen | _ExitMarker | None":
        if key == "esc":
            if self.filter or self.filter_mode:
                self.filter = ""
                self.filter_mode = False
                self.cursor = 0
                return None
            self._cancel()
            return _EXIT
        if key == "backspace":
            if self.filter:
                self.filter = self.filter[:-1]
                self.cursor = 0
            return None
        # space
        if self.filter_mode:
            self.filter += " "
            self.cursor = 0
        else:
            self._toggle_mark()
        return None

    def _handle_nav(self, key: str) -> None:
        if key == "up":
            self._move(-1)
        elif key == "down":
            self._move(+1)
        elif key == "left":
            self._go_up()
        else:
            self._enter()

    def _handle_text(self, key: str) -> "Screen | _ExitMarker | None":
        if key in ("q", "Q") and not self.filter and not self.filter_mode:
            self._cancel()
            return _EXIT
        if len(key) == 1 and key.isprintable():
            self.filter_mode = True
            self.filter += key
            self.cursor = 0
        return None

    # -- actions -------------------------------------------------------- #

    def _move(self, step: int) -> None:
        visible = self._visible()
        if not visible:
            return
        self.cursor = (self.cursor + step) % len(visible)

    def _enter(self) -> None:
        visible = self._visible()
        if not visible:
            return
        target = visible[self.cursor]
        if not _is_enterable(target):
            return
        self.current = target
        self.entries = _list_dir_entries(target)
        self.cursor = 0
        self.filter = ""
        self.filter_mode = False

    def _go_up(self) -> None:
        if self.current == self.origin:
            return
        previous = self.current
        parent = previous.parent
        self.current = parent
        self.entries = _list_dir_entries(parent)
        self.cursor = 0
        try:
            self.cursor = self.entries.index(previous)
        except ValueError:
            self.cursor = 0
        self.filter = ""
        self.filter_mode = False

    def _toggle_mark(self) -> None:
        visible = self._visible()
        if not visible:
            return
        target = visible[self.cursor]
        if target in self.marked:
            self.marked.discard(target)
        else:
            self.marked.add(target)

    def _confirm(self) -> None:
        if self.marked:
            self.result = sorted(self.marked, key=str)
            return
        visible = self._visible()
        self.result = [visible[self.cursor]] if visible else None

    def _cancel(self) -> None:
        self.result = None


def _select_sources_arrow(origin: Path) -> list[Path] | None:
    """Arrow-key file picker restricted to *origin* and its descendants.

    Returns the list of selected paths, or ``None`` if the user cancelled.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    screen = _PickerScreen(origin)
    try:
        _run_screen(screen)
    except KeyboardInterrupt:
        return None
    return screen.result


# =========================================================================== #
#  Responsive enumeration (worker thread + spinner)
# =========================================================================== #

def _read_key_timeout(timeout: float) -> str | None:
    """Non-blocking key read with a timeout. Returns None on timeout."""
    if os.name == "nt":
        import msvcrt
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                return _read_key_windows()
            time.sleep(0.02)
        return None
    import select
    fd = sys.stdin.fileno()
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return None
    return _read_key()


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _draw_spinner_line(frame: str, count: int, total_bytes: int,
                       last_name: str) -> None:
    line = (
        "  " + _paint(frame, _C.BOLD_CYAN) + "  "
        + _paint("Enumerating…", _C.BOLD) + " "
        + f"{count} items, {human_bytes(total_bytes)}"
    )
    if last_name:
        line += "  " + _paint(_truncate(last_name, 40), _C.DIM)
    cols = shutil.get_terminal_size((120, 24)).columns
    if _visible_len(line) > cols - 1:
        line = _truncate(line, cols - 1)
    sys.stdout.write("\r" + line + _CLEAR_LINE)
    sys.stdout.flush()


@dataclass
class _EnumState:
    count: int = 0
    total: int = 0
    last: str = ""


def _enumerate_worker(
    sources, root, symlinks, archive_real,
    q: "queue.Queue[tuple]", cancel: threading.Event, box: list,
) -> None:
    """Run in a worker thread; feed ``(kind, ...)`` messages into ``q``."""
    try:
        items: list[tuple[Path, str]] = []
        total = 0
        for fs_path, name in _iter_items(
            sources, root=root, symlinks=symlinks,
            archive_real=archive_real, cancel=cancel,
        ):
            items.append((fs_path, name))
            total += _file_size(fs_path)
            q.put(("item", len(items), total, name))
        if cancel.is_set():
            q.put(("cancelled",))
        else:
            box[0] = (items, total)
            q.put(("done",))
    except Exception as exc:
        box[0] = exc
        q.put(("error",))


def _drain_queue(q: "queue.Queue[tuple]", state: _EnumState) -> bool:
    """Drain pending messages into *state*. Returns True on terminal message."""
    terminal = False
    try:
        while True:
            msg = q.get_nowait()
            if msg[0] == "item":
                state.count = msg[1]
                state.total = msg[2]
                state.last = msg[3]
            elif msg[0] in ("done", "error", "cancelled"):
                terminal = True
    except queue.Empty:
        pass
    return terminal


def _spinner_loop(q: "queue.Queue[tuple]", state: _EnumState) -> bool:
    """Draw the spinner until the worker finishes. Returns True if cancelled."""
    idx = 0
    with _raw_mode():
        try:
            while True:
                if _drain_queue(q, state):
                    return False
                _draw_spinner_line(
                    _SPINNER_FRAMES[idx % len(_SPINNER_FRAMES)],
                    state.count, state.total, state.last,
                )
                idx += 1
                if _read_key_timeout(0.08) in ("esc", "quit", "q", "Q"):
                    return True
        finally:
            sys.stdout.write("\r" + _CLEAR_LINE)
            sys.stdout.flush()


def _interactive_enumerate(sources, *, root, symlinks, archive_real):
    """Walk the tree in a worker thread, showing a spinner.

    Returns ``(items, total_bytes)`` on success, or ``None`` if the user
    cancelled with Esc, q, or Ctrl+C.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return ArchiveEngine._collect_items(
            sources, root=root, symlinks=symlinks, archive_real=archive_real,
        )

    q: "queue.Queue[tuple]" = queue.Queue()
    cancel = threading.Event()
    box: list = [None]

    t = threading.Thread(
        target=_enumerate_worker,
        args=(sources, root, symlinks, archive_real, q, cancel, box),
        daemon=True,
    )
    t.start()

    state = _EnumState()
    cancelled = False
    try:
        cancelled = _spinner_loop(q, state)
    except KeyboardInterrupt:
        cancelled = True

    if cancelled:
        cancel.set()
    t.join(timeout=2.0)

    if cancelled:
        return None
    result = box[0]
    if isinstance(result, BaseException):
        raise result
    return result


# =========================================================================== #
#  Interactive menu actions
# =========================================================================== #

def _prompt(prompt: str, default: str = "") -> str:
    """Prompt for a line of input, returning *default* on a blank entry."""
    suffix = f" [{default}]" if default else ""
    sys.stdout.write(_SHOW_CURSOR)
    sys.stdout.flush()
    try:
        response = input(f"  {prompt}{suffix}: ").strip()
    except EOFError as exc:
        raise _QuitInteractive() from exc
    except KeyboardInterrupt as exc:
        print()
        raise _QuitInteractive() from exc
    finally:
        sys.stdout.write(_HIDE_CURSOR)
        sys.stdout.flush()
    return response or default


def _ensure_archive_suffix(name: str) -> str:
    if name.lower().endswith(_ARCHIVE_SUFFIXES):
        return name
    return name + ".tar.gz"


def _find_archives(directory: Path) -> list[Path]:
    try:
        candidates = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [
        p for p in candidates
        if p.is_file() and p.name.lower().endswith(_ARCHIVE_SUFFIXES)
    ]


def _build_archive_options(found: list[Path]) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = []
    for i, path in enumerate(found, 1):
        try:
            size = human_bytes(path.stat().st_size)
        except OSError:
            size = "?"
        options.append((str(i), f"{path.name}  ({size})"))
    options.append(("m", "Enter a path manually"))
    options.append(("b", "Back"))
    return options


def _handle_manual_path() -> Path | None:
    _clear_screen()
    print()
    raw = _prompt("Archive path")
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_file():
        return candidate
    print(f"  Not a file: {candidate}")
    return None


def _select_archive() -> Path | None:
    """Arrow-navigable archive picker scoped to the current directory."""
    found = _find_archives(Path.cwd())

    if not found:
        _clear_screen()
        print("\n  (no archives found in the current directory)")
        return _handle_manual_path()

    options = _build_archive_options(found)

    while True:
        choice = _select_menu("Choose an archive", options,
                              subtitle=str(Path.cwd()))
        if choice is None or choice == "b":
            return None
        if choice == "m":
            picked = _handle_manual_path()
            if picked is not None:
                return picked
            continue
        try:
            index = int(choice) - 1
        except ValueError:
            continue
        if 0 <= index < len(found):
            return found[index]


def _display_picked_sources(sources: list[Path], origin: Path) -> None:
    _clear_screen()
    print(f"\n  Selected {len(sources)} source(s):")
    for path in sources:
        try:
            shown = path.relative_to(origin)
        except ValueError:
            shown = path
        print(f"    {shown}")
    print()


def _menu_create(engine: ArchiveEngine) -> None:
    cwd = Path.cwd()
    sources = _select_sources_arrow(cwd)
    if not sources:
        _clear_screen()
        print("\n  No sources selected; cancelled.")
        return

    _display_picked_sources(sources, cwd)

    archive_name = _prompt("Archive name", "archive.tar.gz")
    if not archive_name:
        print("  No archive name given; cancelled.")
        return
    archive_name = _ensure_archive_suffix(archive_name)
    archive_path = cwd / archive_name
    if archive_path.exists():
        answer = _prompt(f"{archive_name} exists. Overwrite? (y/N)", "n").lower()
        if answer not in ("y", "yes"):
            print("  Cancelled; existing archive was not changed.")
            return

    level_raw = _prompt("Compression level (0-9, blank for default)", "")
    compresslevel: int | None = None
    if level_raw:
        try:
            compresslevel = max(0, min(9, int(level_raw)))
        except ValueError:
            print(f"  Ignoring non-numeric level {level_raw!r}.")

    _clear_screen()
    print()
    archive_real = os.path.realpath(archive_path)
    try:
        precollected = _interactive_enumerate(
            sources,
            root=cwd,
            symlinks="store",
            archive_real=archive_real,
        )
    except KeyboardInterrupt:
        _clear_screen()
        print("\n  Cancelled.")
        return
    if precollected is None:
        _clear_screen()
        print("\n  Enumeration cancelled.")
        return

    print()
    display = ProgressDisplay("create")
    report = engine.create(
        archive_path,
        sources,
        root=cwd,
        compresslevel=compresslevel,
        progress=display,
        precollected=precollected,
    )
    print()
    _print_create_summary(report)
    if report.skipped:
        for name in report.skipped:
            print(f"  skipped: {name}", file=sys.stderr)


def _menu_extract(engine: ArchiveEngine) -> None:
    archive = _select_archive()
    if archive is None:
        _clear_screen()
        print("\n  Cancelled.")
        return

    _clear_screen()
    print(f"\n  Extracting {archive.name} into the current directory…\n")
    display = ProgressDisplay("extract")
    report = engine.extract(archive, ".", progress=display)
    print()
    _print_extract_summary(report)
    if report.skipped:
        for name in report.skipped:
            print(f"  skipped: {name}", file=sys.stderr)


def _menu_list(engine: ArchiveEngine) -> None:
    archive = _select_archive()
    if archive is None:
        _clear_screen()
        print("\n  Cancelled.")
        return

    _clear_screen()
    entries = engine.list_entries(archive)
    print(f"\n  {archive.name} — {len(entries)} entries\n")
    for entry in entries:
        _print_entry_verbose(entry)
    print()


def _menu_info(engine: ArchiveEngine) -> None:
    archive = _select_archive()
    if archive is None:
        _clear_screen()
        print("\n  Cancelled.")
        return

    _clear_screen()
    data = engine.info(archive)
    width = max(len(k) for k in data)
    print()
    for key, value in data.items():
        if key in ("archive_size", "uncompressed_size"):
            value = human_bytes(value)
        print(f"  {key:<{width}} : {value}")
    print()


# -- top-level loop --------------------------------------------------------- #

_MENU_OPTIONS: list[tuple[str, str]] = [
    ("1", "Create an archive"),
    ("2", "Extract an archive"),
    ("3", "List archive contents"),
    ("4", "Show archive info"),
    ("0", "Quit"),
]

_MENU_HANDLERS: dict[str, Callable[[ArchiveEngine], None]] = {
    "1": _menu_create,
    "2": _menu_extract,
    "3": _menu_list,
    "4": _menu_info,
}


def _run_menu_handler(engine: ArchiveEngine,
                      handler: Callable[[ArchiveEngine], None]) -> int | None:
    """Invoke a menu handler, catch its errors, then pause."""
    try:
        handler(engine)
    except _QuitInteractive:
        return 0
    except UnsafeArchiveError as exc:
        _clear_screen()
        print(f"\n  error: unsafe archive: {exc}\n")
    except ArchiveError as exc:
        _clear_screen()
        print(f"\n  error: {exc}\n")
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
        _clear_screen()
        print(f"\n  error: {exc}\n")

    try:
        _wait_for_enter()
    except KeyboardInterrupt:
        return 130

    return None


def _run_menu_choice(engine: ArchiveEngine) -> int | None:
    """Run one pass of the top-level menu."""
    try:
        choice = _select_menu(
            "Select an action",
            _MENU_OPTIONS,
            subtitle=f"  Working directory: {Path.cwd()}",
        )
    except KeyboardInterrupt:
        return 130

    if choice is None or choice == "0":
        return 0

    handler = _MENU_HANDLERS.get(choice)
    if handler is None:
        return None

    return _run_menu_handler(engine, handler)


def _interactive_menu() -> int:
    """Run the Snug interactive shell.  Returns a process exit code."""
    _enable_ansi_windows()
    _init_color()
    engine = ArchiveEngine()

    with _fullscreen():
        try:
            while True:
                code = _run_menu_choice(engine)
                if code is not None:
                    return code
        except KeyboardInterrupt:
            return 130


def _launch_interactive() -> int:
    """Entry point used when ``snug`` is called with no arguments."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _build_parser().print_help()
        return 1
    return _interactive_menu()


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snug",
        description="Create, inspect and extract archives "
                    "with live speed and ETA.  "
                    "Run without arguments for an interactive menu.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    formats = [f.value for f in ArchiveFormat]

    c = sub.add_parser("create", help="create an archive")
    c.add_argument("archive", help="path of the archive to write")
    c.add_argument("sources", nargs="+", help="files and/or directories to add")
    c.add_argument("-f", "--format", choices=formats, default=None,
                   help="force the archive format")
    c.add_argument("-r", "--root", default=None,
                   help="store paths relative to this directory")
    c.add_argument("-l", "--level", type=int, choices=range(10), default=None,
                   help="compression level (0-9)")
    c.add_argument("--symlinks", choices=["store", "follow", "skip"],
                   default="store")
    c.add_argument("--force", action="store_true",
                   help="replace an existing archive")
    c.add_argument("-q", "--quiet", action="store_true",
                   help="suppress progress and summary output")

    x = sub.add_parser("extract", help="extract an archive")
    x.add_argument("archive", help="archive to extract")
    x.add_argument("-C", "--directory", default=".", metavar="DIR",
                   help="destination directory (default: .)")
    x.add_argument("-m", "--member", action="append", default=None,
                   help="extract only this member (repeatable)")
    x.add_argument("--strip-components", type=int, default=0, metavar="N",
                   help="drop N leading path components")
    x.add_argument("--no-overwrite", action="store_true",
                   help="leave existing files untouched")
    x.add_argument("--no-metadata", action="store_true",
                   help="do not restore permissions / timestamps")
    x.add_argument("--symlinks", choices=["store", "skip"], default="store")
    x.add_argument("-q", "--quiet", action="store_true")

    l = sub.add_parser("list", help="list archive contents")
    l.add_argument("archive")
    l.add_argument("-v", "--verbose", action="store_true",
                   help="show size, mode and modification time")

    i = sub.add_parser("info", help="show archive summary")
    i.add_argument("archive")

    return parser


# -- summary printers ------------------------------------------------------- #

def _print_create_summary(report: CreateReport) -> None:
    ratio = f"{report.ratio * 100:.1f}% of input" if report.bytes_in else "n/a"
    print(f"Created {report.archive}  [{report.format.value}]")
    print(f"  entries    : {report.entries} "
          f"({report.files} files, {report.directories} dirs, "
          f"{report.symlinks} links)")
    print(f"  input      : {human_bytes(report.bytes_in)}")
    print(f"  output     : {human_bytes(report.bytes_out)}  ({ratio})")
    print(f"  elapsed    : {human_time(report.elapsed)}")
    print(f"  avg speed  : {human_bytes(report.avg_speed)}/s")


def _print_extract_summary(report: ExtractReport) -> None:
    print(f"Extracted {report.total} items to {report.destination}")
    print(f"  files      : {report.files}")
    print(f"  directories: {report.directories}")
    print(f"  symlinks   : {report.symlinks}")
    print(f"  bytes      : {human_bytes(report.bytes_written)}")
    print(f"  elapsed    : {human_time(report.elapsed)}")
    print(f"  avg speed  : {human_bytes(report.avg_speed)}/s")
    if report.skipped:
        print(f"  skipped    : {len(report.skipped)} item(s)")


def _entry_kind(entry: ArchiveEntry) -> str:
    if entry.is_dir:
        return "d"
    if entry.is_symlink:
        return "l"
    return "-"


def _format_entry_verbose(entry: ArchiveEntry) -> str:
    mode = f"{entry.mode:04o}" if entry.mode is not None else "----"
    when = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime))
        if entry.mtime else " " * 16
    )
    name = _safe(entry.name)
    extra = f" -> {_safe(entry.link_target)}" if entry.link_target else ""
    return (
        f"  {_entry_kind(entry)}{mode} {entry.size:>12,}  "
        f"{when}  {name}{extra}"
    )


def _print_entry_verbose(entry: ArchiveEntry) -> None:
    print(_format_entry_verbose(entry))


# -- per-command handlers --------------------------------------------------- #

def _cmd_create(args, engine: ArchiveEngine) -> None:
    archive_path = Path(args.archive)
    if archive_path.exists() and not args.force:
        raise ArchiveError(
            f"archive already exists: {archive_path} (use --force to replace it)"
        )
    display = ProgressDisplay("create", quiet=args.quiet)
    report = engine.create(
        args.archive,
        args.sources,
        fmt=args.format,
        root=args.root,
        compresslevel=args.level,
        symlinks=args.symlinks,
        progress=display,
    )
    if args.quiet:
        return
    _print_create_summary(report)
    for name in report.skipped:
        print(f"  skipped: {name}", file=sys.stderr)


def _cmd_extract(args, engine: ArchiveEngine) -> None:
    display = ProgressDisplay("extract", quiet=args.quiet)
    report = engine.extract(
        args.archive,
        args.directory,
        members=args.member,
        overwrite=not args.no_overwrite,
        preserve_metadata=not args.no_metadata,
        symlinks=args.symlinks,
        strip_components=args.strip_components,
        progress=display,
    )
    if args.quiet:
        return
    _print_extract_summary(report)
    for name in report.skipped:
        print(f"  skipped: {name}", file=sys.stderr)


def _cmd_list(args, engine: ArchiveEngine) -> None:
    for entry in engine.list_entries(args.archive):
        if args.verbose:
            _print_entry_verbose(entry)
        else:
            print(_safe(entry.name))


def _cmd_info(args, engine: ArchiveEngine) -> None:
    data = engine.info(args.archive)
    width = max(len(k) for k in data)
    for key, value in data.items():
        if key in ("archive_size", "uncompressed_size"):
            value = human_bytes(value)
        print(f"{key:<{width}} : {value}")


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)

    if not argv:
        return _launch_interactive()

    args = _build_parser().parse_args(argv)
    engine = ArchiveEngine()

    dispatch = {
        "create": _cmd_create,
        "extract": _cmd_extract,
        "list": _cmd_list,
        "info": _cmd_info,
    }

    try:
        dispatch[args.command](args, engine)
    except UnsafeArchiveError as exc:
        print(f"error: unsafe archive: {exc}", file=sys.stderr)
        return 3
    except ArchiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())