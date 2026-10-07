"""Archive formats, safe streaming I/O and backend orchestration for Snug."""

from __future__ import annotations

import argparse
import bz2
import gzip
import io
import lzma
import math
import os
import queue
import re
import secrets
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Literal, Protocol, Sequence

__version__ = "1.8.0"

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


def _safe(text: object) -> str:
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


class ResourceLimitError(ArchiveError):
    """An explicit extraction quota was exceeded."""


@dataclass(frozen=True)
class ExtractionLimits:
    max_entries: int | None = None
    max_total_size: int | None = None
    max_file_size: int | None = None
    max_ratio: float | None = None

    def __post_init__(self):
        for value in (self.max_entries, self.max_total_size, self.max_file_size):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('extraction counts and sizes must be nonnegative integers')
        if self.max_ratio is not None and (isinstance(self.max_ratio, bool)
                or not math.isfinite(self.max_ratio) or self.max_ratio <= 0):
            raise ValueError('maximum ratio must be a finite positive number')


def parse_size(text: str) -> int:
    """Parse exact byte amounts: decimal K/KB and binary KiB through TiB."""
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(B|[KMGT](?:B|iB)?)?', text) if len(text) <= 128 else None
    if match is None:
        raise ValueError('size must be bytes or a decimal K/KB/MB/GB/TB or binary KiB/MiB/GiB/TiB amount')
    number, unit = match.groups()
    power = 'KMGT'.index(unit[0]) + 1 if unit and unit[0] in 'KMGT' else 0
    try:
        with localcontext() as context:
            context.prec = 256
            size = Decimal(number) * ((1024 if unit and unit.endswith('iB') else 1000) ** power)
        if size != size.to_integral_value():
            raise ValueError('size must resolve to an exact whole number of bytes')
        return int(size)
    except InvalidOperation as exc:
        raise ValueError('invalid size') from exc


class _ExtractionBudget:
    def __init__(self, limits: ExtractionLimits | None = None):
        self.limits = limits or ExtractionLimits()
        self.total = 0

    def preflight(self, entries: Sequence[ArchiveEntry]) -> None:
        limits = self.limits
        if limits.max_entries is not None and len(entries) > limits.max_entries:
            raise ResourceLimitError(f'archive contains {len(entries)} selected entries; limit is {limits.max_entries}')
        total = 0
        for entry in entries:
            if entry.is_dir or entry.is_hardlink or not entry.size_known:
                continue
            if entry.size < 0:
                raise ArchiveError(f'invalid declared member size: {entry.name!r}')
            total += entry.size
            self._file(entry, entry.size)
        if limits.max_total_size is not None and total > limits.max_total_size:
            raise ResourceLimitError(f'selected archive payload declares {total} bytes; total limit is {limits.max_total_size}')

    def _file(self, entry: ArchiveEntry, size: int) -> None:
        limits = self.limits
        if limits.max_file_size is not None and size > limits.max_file_size:
            raise ResourceLimitError(f'member {entry.name!r} exceeds file limit {limits.max_file_size} bytes')
        compressed = entry.compressed_size
        if limits.max_ratio is not None and compressed is not None and compressed >= 0:
            if (compressed == 0 and size > 0) or (compressed > 0 and size / compressed > limits.max_ratio):
                raise ResourceLimitError(f'member {entry.name!r} exceeds compression ratio limit {limits.max_ratio:g}')

    def consume(self, entry: ArchiveEntry, previous: int, count: int) -> None:
        self._file(entry, previous + count)
        if self.limits.max_total_size is not None and self.total + count > self.limits.max_total_size:
            raise ResourceLimitError(f'member {entry.name!r} exceeds total limit {self.limits.max_total_size} bytes')
        self.total += count


class _QuitInteractive(Exception):
    """Raised when the user chooses to leave the interactive menu."""


# --------------------------------------------------------------------------- #
#  Formats
# --------------------------------------------------------------------------- #

class ArchiveFormat(str, Enum):
    ZIP = "zip"
    ZIPX = "zipx"
    TAR = "tar"
    TAR_GZ = "tar.gz"
    TAR_BZ2 = "tar.bz2"
    TAR_XZ = "tar.xz"
    SEVEN_ZIP = "7z"
    RAR = "rar"
    RAR5 = "rar5"
    GZIP = "gz"
    BZIP2 = "bz2"
    XZ = "xz"
    LZMA = "lzma"
    CAB = "cab"
    ISO = "iso"
    CPIO = "cpio"
    AR = "ar"
    XAR = "xar"
    LHA = "lha"
    LZH = "lzh"
    WARC = "warc"
    RPM = "rpm"
    DEB = "deb"

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
    (".zipx", ArchiveFormat.ZIPX),
    (".7z", ArchiveFormat.SEVEN_ZIP),
    (".rar5", ArchiveFormat.RAR5),
    (".rar", ArchiveFormat.RAR),
    (".gz", ArchiveFormat.GZIP),
    (".gzip", ArchiveFormat.GZIP),
    (".bz2", ArchiveFormat.BZIP2),
    (".bzip2", ArchiveFormat.BZIP2),
    (".xz", ArchiveFormat.XZ),
    (".lzma", ArchiveFormat.LZMA),
    (".cab", ArchiveFormat.CAB),
    (".iso", ArchiveFormat.ISO),
    (".cpio", ArchiveFormat.CPIO),
    (".ar", ArchiveFormat.AR),
    (".a", ArchiveFormat.AR),
    (".xar", ArchiveFormat.XAR),
    (".lha", ArchiveFormat.LHA),
    (".lzh", ArchiveFormat.LZH),
    (".warc", ArchiveFormat.WARC),
    (".rpm", ArchiveFormat.RPM),
    (".deb", ArchiveFormat.DEB),
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
    if not for_write and path.is_file():
        fmt = _detect_by_magic(path)
        if fmt is not None:
            return fmt
        fmt = _detect_with_backend(path)
        if fmt is not None:
            return fmt
    fmt = _extension_format(path)
    if fmt is not None:
        return fmt
    hint = " (use -f/--format to specify one)" if for_write else ""
    raise FormatError(f"cannot determine archive format for {path.name!r}{hint}")


def _detect_with_backend(path: Path) -> ArchiveFormat | None:
    # Detection hooks must not recurse into detect_format().
    for backend in _backends():
        probe = getattr(backend, "detect", None)
        if probe is None or not backend.available():
            continue
        fmt = probe(path)
        if fmt is not None:
            return _normalize_format(fmt)
    return None


def _extension_format(path: Path) -> ArchiveFormat | None:
    name = path.name.lower()
    return next((fmt for suffix, fmt in _SUFFIX_MAP if name.endswith(suffix)), None)


def _is_tar_header(head: bytes) -> bool:
    if len(head) < 512:
        return False
    if head[257:262] == b"ustar":
        return True
    # Old-style TAR headers have no magic; validate their octal checksum.
    try:
        expected = int(head[148:156].strip(b"\0 ") or b"0", 8)
    except ValueError:
        return False
    return expected != 0 and expected == sum(head[:148]) + 8 * 32 + sum(head[156:512])


def _compressed_format(path: Path, stream, standalone: ArchiveFormat,
                       container: ArchiveFormat | None) -> ArchiveFormat:
    try:
        with stream(path, "rb") as fh:
            head = fh.read(512)
        if container is not None and (_is_tar_header(head) or
                                     (head == b"\0" * 512 and _extension_format(path) is container)):
            return container
    except (OSError, EOFError, lzma.LZMAError, zlib.error):
        pass
    return standalone


def _detect_zip_format(path: Path) -> ArchiveFormat:
    try:
        with zipfile.ZipFile(path) as zf:
            if any(i.compress_type not in _native_zip_methods() for i in zf.infolist()):
                return ArchiveFormat.ZIPX
    except (OSError, zipfile.BadZipFile):
        pass
    return ArchiveFormat.ZIPX if _extension_format(path) is ArchiveFormat.ZIPX else ArchiveFormat.ZIP


_MAGIC_FORMATS = {
    b"7z\xbc\xaf\x27\x1c": ArchiveFormat.SEVEN_ZIP,
    b"Rar!\x1a\x07\x01\x00": ArchiveFormat.RAR5,
    b"Rar!\x1a\x07\x00": ArchiveFormat.RAR,
    b"MSCF": ArchiveFormat.CAB, b"xar!": ArchiveFormat.XAR,
    b"\xed\xab\xee\xdb": ArchiveFormat.RPM, b"WARC/": ArchiveFormat.WARC,
    b"070701": ArchiveFormat.CPIO, b"070702": ArchiveFormat.CPIO,
    b"070707": ArchiveFormat.CPIO, b"\xc7\x71": ArchiveFormat.CPIO,
    b"\x71\xc7": ArchiveFormat.CPIO,
}


def _header_format(path: Path, head: bytes) -> ArchiveFormat | None:
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return _detect_zip_format(path)
    compressed = (
        (b"\x1f\x8b", gzip.open, ArchiveFormat.GZIP, ArchiveFormat.TAR_GZ),
        (b"BZh", bz2.open, ArchiveFormat.BZIP2, ArchiveFormat.TAR_BZ2),
        (b"\xfd7zXZ\x00", lzma.open, ArchiveFormat.XZ, ArchiveFormat.TAR_XZ),
    )
    for magic, opener, standalone, container in compressed:
        if head.startswith(magic):
            return _compressed_format(path, opener, standalone, container)
    for magic, fmt in _MAGIC_FORMATS.items():
        if head.startswith(magic):
            return fmt
    if head.startswith(b"!<arch>\n"):
        return ArchiveFormat.DEB if head[8:24].rstrip() == b"debian-binary/" else ArchiveFormat.AR
    if head.startswith(b"!<thin>\n"):
        raise FormatError("thin AR archives reference external files and are unsupported")
    if len(head) >= 7 and head[2:5] in (b"-lh", b"-lz") and head[6:7] == b"-":
        return ArchiveFormat.LHA
    return None


def _detect_lzma_format(path: Path, head: bytes) -> ArchiveFormat | None:
    # LZMA-alone lacks a unique magic. Require a plausible header and a
    # successful bounded decoder probe instead of trusting one byte.
    if len(head) < 13 or head[0] >= 225:
        return None
    dictionary = int.from_bytes(head[1:5], "little")
    if dictionary not in {1 << n for n in range(12, 31)} | {3 << n for n in range(11, 30)}:
        return None
    try:
        with lzma.open(path, "rb") as fh:
            fh.read(512)
        return ArchiveFormat.LZMA
    except (OSError, EOFError, lzma.LZMAError):
        return None


def _detect_by_magic(path: Path) -> ArchiveFormat | None:
    try:
        with path.open("rb") as fh:
            head = fh.read(512)
            fmt = _header_format(path, head)
            if fmt is not None:
                return fmt
            fh.seek(32769)
            if fh.read(5) == b"CD001":
                return ArchiveFormat.ISO
            if _is_tar_header(head) or (head == b"\0" * 512 and path.stat().st_size >= 1024):
                return ArchiveFormat.TAR
        return _detect_lzma_format(path, head)
    except OSError:
        return None


def _normalize_format(fmt) -> ArchiveFormat:
    if isinstance(fmt, ArchiveFormat):
        return fmt
    value = str(fmt).lower().lstrip(".")
    aliases = {"gzip": "gz", "bzip2": "bz2", "tgz": "tar.gz", "tbz": "tar.bz2",
               "tbz2": "tar.bz2", "txz": "tar.xz", "seven_zip": "7z"}
    return ArchiveFormat(aliases.get(value, value))


def _native_zip_methods() -> set[int]:
    supported = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                 zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
    zstandard = getattr(zipfile, "ZIP_ZSTANDARD", None)
    if zstandard is not None:
        supported.update((20, zstandard))
    return supported


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

        counters = (
            f"| {human_bytes(self.bytes_done):>9}/{human_bytes(self.total_bytes):<9} "
            f"| {human_bytes(speed):>9}/s | ETA {human_time(eta)} "
            if self.total_bytes else
            f"| {self.items_done}/{self.total_items} items | elapsed {human_time(elapsed)} "
        )
        line = (
            f"{_paint(f'{self.operation:>7}', _C.BOLD_CYAN)} |{bar}| "
            f"{frac * 100:5.1f}% "
            f"{counters}"
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
        detail = (f", {human_bytes(self.bytes_done)} in {human_time(elapsed)} "
                  f"(avg {human_bytes(speed)}/s)" if self.total_bytes else
                  f" in {human_time(elapsed)}")
        self.stream.write(f"{self.operation}: {self.items_done}/{self.total_items} items{detail}\n")
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
    if name.startswith("/") or _DRIVE_RE.match(name) or "\0" in name:
        raise UnsafeArchiveError(f"absolute or invalid path in member: {name!r}")
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
        _validate_windows_parts(parts, name)
    return parts


def _validate_windows_parts(parts: list[str], name: str) -> None:
    for part in parts:
        stem = part.split(".", 1)[0].upper()
        if part.endswith((".", " ")) or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem):
            raise UnsafeArchiveError(f"unsafe Windows path component in member: {name!r}")
        if ":" in part:
            raise UnsafeArchiveError(f"alternate data stream in member: {name!r}")


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
    _check_inside(dest_real, parent)
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveError(f"cannot create directory {parent}: {exc}") from exc
    _check_inside(dest_real, parent)


def _check_inside(dest_real: Path, path: Path) -> None:
    """Check resolved paths before creating directories or applying metadata."""
    real_parent = Path(os.path.realpath(path))
    try:
        real_parent.relative_to(dest_real)
    except ValueError:
        raise UnsafeArchiveError(
            f"refusing to write outside destination through a symlink: {path}"
        ) from None


def _make_symlink(
    dest_real: Path, target: Path, link_target: str, overwrite: bool = True
) -> bool:
    """Create a symlink only when its resolved target stays inside dest_real."""
    _check_inside(dest_real, target.parent)
    normalized = link_target.replace("\\", "/")
    if not normalized:
        raise UnsafeArchiveError(f"refusing to create symlink with an empty target: {target.name!r}")
    if PurePosixPath(normalized).is_absolute() or _DRIVE_RE.match(normalized) or "\0" in normalized:
        raise UnsafeArchiveError(
            f"refusing to create absolute symlink "
            f"{target.name!r} -> {link_target!r}"
        )
    if os.name == "nt":
        for part in normalized.split("/"):
            if part not in ("", ".", ".."):
                _clean_parts(part)

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
    except (OSError, ValueError, OverflowError):
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
    except OSError as exc:
        seen.discard(real)
        raise ArchiveError(f"cannot enumerate directory {path}: {exc}") from exc
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
    is_hardlink: bool = False
    size_known: bool = True


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
    budget: _ExtractionBudget = field(default_factory=_ExtractionBudget)
    deferred_dirs: list[tuple[Path, int | None, float | None]] = field(default_factory=list)
    pending_outputs: list[SafeOutputFile] = field(default_factory=list)
    deferred_hardlinks: list[tuple[ArchiveEntry, Path, str | None]] = field(default_factory=list)


class SafeOutputFile:
    """Stage bounded chunks; publish only a sealed, validated regular file.

    A no-overwrite publication uses an exclusive hard link. Filesystems without
    that operation fail closed, rather than exposing an exclusive-create copy
    while it is incomplete. Parent directories must not be concurrently moved
    by an untrusted process; portable path checks are not a filesystem sandbox.
    """

    def __init__(self, ctx: _ExtractContext, entry: ArchiveEntry, target: Path):
        self.ctx, self.entry, self.target = ctx, entry, target
        self.length = 0
        self.handle = None
        self.temporary: Path | None = None
        self.committed = False
        self.skipped = False
        _prepare_target(ctx.dest_real, target)
        if not ctx.overwrite and os.path.lexists(target):
            self.skipped = True
            ctx.report.skipped.append(entry.name)
            return
        if target.is_dir() and not target.is_symlink():
            raise ArchiveError(f"cannot atomically replace directory with file: {entry.name!r}")
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        # A fixed, short basename also works for NAME_MAX-length destinations.
        for _ in range(32):
            temporary = target.parent / ('.snug-part-' + secrets.token_hex(12))
            try:
                fd = os.open(temporary, flags, 0o600)
            except FileExistsError:
                continue
            self.temporary = temporary
            try:
                self.handle = os.fdopen(fd, 'w+b')
            except BaseException:
                os.close(fd)
                temporary.unlink(missing_ok=True)
                raise
            break
        if self.handle is None:
            raise ArchiveError(f"cannot create extraction staging file: {entry.name!r}")
        ctx.pending_outputs.append(self)

    def write(self, data: bytes | bytearray) -> int:
        if self.skipped:
            return len(data)
        if self.handle is None or self.handle.closed:
            raise ArchiveError(f"extraction writer is closed: {self.entry.name!r}")
        self.ctx.budget.consume(self.entry, self.length, len(data))
        count = self.handle.write(data)
        if count != len(data):
            raise ArchiveError(f"incomplete output write: {self.entry.name!r}")
        self.length += count
        self.ctx.progress.chunk(count)
        self.ctx.report.bytes_written += count
        return count

    def seal(self) -> None:
        if self.skipped or self.handle is None or self.handle.closed:
            return
        try:
            self.handle.flush()
            try:
                os.fsync(self.handle.fileno())
            except OSError:
                pass
        finally:
            self.handle.close()
        if self.entry.size_known and self.length != self.entry.size:
            raise ArchiveError(f"incomplete archive member {self.entry.name!r}: expected {self.entry.size} bytes, read {self.length}")

    def commit(self) -> None:
        if self.skipped or self.committed or self.temporary is None:
            return
        self.seal()
        _check_inside(self.ctx.dest_real, self.target.parent)
        if self.target.is_dir() and not self.target.is_symlink():
            if not self.ctx.overwrite:
                self.skipped = True
                self.ctx.report.skipped.append(self.entry.name)
                self.abort()
                return
            raise ArchiveError(f"cannot atomically replace directory with file: {self.entry.name!r}")
        if self.ctx.overwrite:
            os.replace(self.temporary, self.target)
        else:
            try:
                os.link(self.temporary, self.target)
            except FileExistsError:
                self.skipped = True
                self.ctx.report.skipped.append(self.entry.name)
                self.abort()
                return
            except OSError as exc:
                raise ArchiveError(f"exclusive atomic file publication is unavailable for {self.entry.name!r}: {exc}") from exc
            self.temporary.unlink()
        self.committed = True
        _apply_metadata(self.target, self.entry.mode, self.entry.mtime, self.ctx.preserve_metadata)
        self.ctx.report.files += 1

    def abort(self) -> None:
        try:
            if self.handle is not None and not self.handle.closed:
                self.handle.close()
        finally:
            if self.temporary is not None:
                self.temporary.unlink(missing_ok=True)


def _extract_entry(ctx: _ExtractContext, entry: ArchiveEntry, chunks=None,
                   hardlink: str | None = None) -> None:
    """Shared checked extraction for backends yielding bounded byte chunks."""
    target = _resolve_member(ctx.dest_real, entry.name, ctx.strip)
    if target is None:
        return
    if entry.is_symlink and ctx.symlinks == "skip":
        ctx.report.skipped.append(entry.name)
        return
    _prepare_target(ctx.dest_real, target)
    if entry.is_dir:
        _extract_directory(ctx, entry, target)
    elif entry.is_symlink:
        _extract_symlink_entry(ctx, entry, target)
    elif hardlink is not None or entry.is_hardlink:
        _extract_hardlink(ctx, entry, target, hardlink)
    else:
        _extract_regular_file(ctx, entry, target, chunks)


def _extract_directory(ctx: _ExtractContext, entry: ArchiveEntry, target: Path) -> None:
    # A pre-existing symlink may never be used as an output directory.
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        if not _prepare_file_target(target, ctx.overwrite):
            ctx.report.skipped.append(entry.name)
            return
    _check_inside(ctx.dest_real, target)
    target.mkdir(parents=True, exist_ok=True)
    ctx.deferred_dirs.append((target, entry.mode, entry.mtime))
    ctx.report.directories += 1


def _extract_symlink_entry(ctx: _ExtractContext, entry: ArchiveEntry, target: Path) -> None:
    if entry.link_target is None:
        raise ArchiveError(f"missing symlink target for {entry.name!r}")
    if _make_symlink(ctx.dest_real, target, entry.link_target, ctx.overwrite):
        ctx.report.symlinks += 1
    else:
        ctx.report.skipped.append(entry.name)


def _extract_hardlink(ctx: _ExtractContext, entry: ArchiveEntry, target: Path,
                      hardlink: str | None) -> None:
    ctx.deferred_hardlinks.append((entry, target, hardlink))


def _commit_hardlink(ctx: _ExtractContext, entry: ArchiveEntry, target: Path,
                     hardlink: str | None) -> None:
    name = hardlink if hardlink is not None else entry.link_target
    source = _resolve_member(ctx.dest_real, name or "", ctx.strip)
    if source is None:
        ctx.report.skipped.append(entry.name)
        return
    _check_inside(ctx.dest_real, source)
    source = Path(os.path.realpath(source))
    if source == Path(os.path.realpath(target)) or not source.is_file():
        ctx.report.skipped.append(entry.name)
        return
    if not _prepare_file_target(target, ctx.overwrite):
        ctx.report.skipped.append(entry.name)
        return
    try:
        os.link(source, target)
    except OSError as exc:
        raise ArchiveError(f"cannot create hardlink {entry.name!r}: {exc}") from exc
    ctx.report.files += 1


def _extract_regular_file(ctx: _ExtractContext, entry: ArchiveEntry, target: Path,
                          chunks: Iterable[bytes] | None) -> None:
    output = SafeOutputFile(ctx, entry, target)
    if output.skipped:
        return
    if chunks is None:
        raise ArchiveError(f"no data reader for {entry.name!r}")
    for buf in chunks:
        output.write(buf)
    output.seal()


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

class ArchiveBackend(Protocol):
    """Optional backends share streamed I/O and the engine's lifecycle."""

    name: str
    read_formats: set[ArchiveFormat]
    write_formats: set[ArchiveFormat]

    def available(self) -> bool: ...
    def can_read(self, path: Path) -> bool: ...
    def can_write(self, fmt: ArchiveFormat) -> bool: ...
    def list_entries(self, path: Path, password=None) -> list[ArchiveEntry]: ...
    def create(self, archive_path: Path, items, fmt: ArchiveFormat,
               compresslevel, symlinks: str, report: CreateReport,
               progress: ProgressSink, password=None) -> None: ...
    def extract(self, archive_path: Path, ctx: _ExtractContext, password=None) -> None: ...
    def metadata(self, path: Path, password=None) -> dict: ...


def _password_bytes(password) -> bytes | None:
    if password is None or isinstance(password, bytes):
        return password
    return str(password).encode("utf-8")


class NativeBackend:
    """Standard-library ZIP and TAR containers, with bounded member I/O."""

    name = "native"
    read_formats = {ArchiveFormat.ZIP, ArchiveFormat.ZIPX, *_TAR_WRITE_MODE}
    write_formats = {ArchiveFormat.ZIP, *_TAR_WRITE_MODE}

    @staticmethod
    def available() -> bool:
        return True

    def can_write(self, fmt: ArchiveFormat) -> bool:
        return fmt in self.write_formats

    def can_read(self, path: Path) -> bool:
        fmt = _detect_by_magic(path) or _extension_format(path)
        if fmt in _TAR_WRITE_MODE:
            return True
        if fmt in (ArchiveFormat.ZIP, ArchiveFormat.ZIPX):
            try:
                with zipfile.ZipFile(path) as zf:
                    return all(i.compress_type in _native_zip_methods() for i in zf.infolist())
            except zipfile.BadZipFile as exc:
                # A tolerant fallback may read local records from a truncated
                # ZIP while silently accepting its missing central directory.
                raise ArchiveError(f"invalid ZIP structure: {exc}") from exc
            except OSError:
                return False
        return False

    def list_entries(self, path: Path, password=None) -> list[ArchiveEntry]:
        fmt = _detect_by_magic(path) or _extension_format(path)
        if fmt in (ArchiveFormat.ZIP, ArchiveFormat.ZIPX):
            with zipfile.ZipFile(path) as zf:
                return [_zip_entry(i) for i in zf.infolist()]
        if password is not None:
            raise ArchiveError("TAR archives do not support passwords")
        with tarfile.open(path, "r:*") as tf:
            return [_tar_entry(m) for m in tf.getmembers()]

    def metadata(self, path: Path, password=None) -> dict:
        fmt = _detect_by_magic(path) or _extension_format(path)
        if fmt in (ArchiveFormat.ZIP, ArchiveFormat.ZIPX):
            with zipfile.ZipFile(path) as zf:
                return {"encrypted": any(i.flag_bits & 1 for i in zf.infolist())}
        if password is not None:
            raise ArchiveError("TAR archives do not support passwords")
        return {"encrypted": False}

    def create(self, archive_path, items, fmt, compresslevel, symlinks,
               report, progress, password=None) -> None:
        if password is not None:
            raise ArchiveError(f"password-protected {fmt.value} creation is not supported by the native backend")
        if fmt is ArchiveFormat.ZIP:
            self._write_zip(archive_path, items, compresslevel, symlinks, report, progress)
        else:
            self._write_tar(archive_path, items, fmt, compresslevel, symlinks, report, progress)

    def extract(self, archive_path, ctx: _ExtractContext, password=None) -> None:
        fmt = _detect_by_magic(archive_path) or _extension_format(archive_path)
        if fmt in (ArchiveFormat.ZIP, ArchiveFormat.ZIPX):
            self._extract_zip(archive_path, ctx, password)
        else:
            if password is not None:
                raise ArchiveError("TAR archives do not support passwords")
            self._extract_tar(archive_path, ctx)

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
        # Python 3.13 made this setting public. Older supported runtimes
        # require the equivalent private slot for streamed ZipInfo writes.
        level_attribute = "compress_level" if hasattr(zi, "compress_level") else "_compresslevel"
        setattr(zi, level_attribute, zf.compresslevel)
        zi.file_size = st.st_size
        with open(fs_path, "rb") as src, zf.open(zi, "w", force_zip64=st.st_size >= zipfile.ZIP64_LIMIT) as dst:
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

    # -- ZIP extraction -------------------------------------------------- #

    def _extract_zip(self, archive_path, ctx: _ExtractContext, password=None) -> None:
        with zipfile.ZipFile(archive_path) as zf:
            infos = [i for i in zf.infolist() if _selected(i.filename, ctx.members)]
            for info in infos:
                _resolve_member(ctx.dest_real, info.filename, ctx.strip)
            total_bytes = sum(i.file_size for i in infos if not i.is_dir()
                              and not stat.S_ISLNK(i.external_attr >> 16))
            ctx.progress.start(total_bytes, len(infos))
            for info in infos:
                ctx.progress.item(info.filename)
                self._extract_zip_member(zf, info, ctx, password)

    def _extract_zip_member(self, zf, info: zipfile.ZipInfo,
                            ctx: _ExtractContext, password=None) -> None:
        target = _resolve_member(ctx.dest_real, info.filename, ctx.strip)
        if target is None:
            return

        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            self._extract_zip_symlink(zf, info, target, ctx, password)
            return
        if info.is_dir() or stat.S_ISDIR(mode):
            self._extract_zip_dir(info, target, mode, ctx)
            return
        self._extract_zip_file(zf, info, target, mode, ctx, password)

    @staticmethod
    def _extract_zip_symlink(zf, info, target, ctx: _ExtractContext, password=None) -> None:
        if ctx.symlinks == "skip":
            ctx.report.skipped.append(info.filename)
            return
        if info.file_size > 65536:
            raise UnsafeArchiveError(f"symlink target is too large: {info.filename!r}")
        payload = zf.read(info, pwd=_password_bytes(password))
        ctx.budget.consume(_zip_entry(info), 0, len(payload))
        link_target = payload.decode("utf-8", "surrogateescape")
        _prepare_target(ctx.dest_real, target)
        if _make_symlink(ctx.dest_real, target, link_target, ctx.overwrite):
            ctx.report.symlinks += 1
        else:
            ctx.report.skipped.append(info.filename)

    @staticmethod
    def _extract_zip_dir(info, target, mode, ctx: _ExtractContext) -> None:
        _extract_entry(ctx, _zip_entry(info))

    @staticmethod
    def _extract_zip_file(zf, info, target, mode, ctx: _ExtractContext, password=None) -> None:
        def chunks():
            with zf.open(info, pwd=_password_bytes(password)) as src:
                yield from iter(lambda: src.read(CHUNK_SIZE), b'')
        _extract_entry(ctx, _zip_entry(info), chunks=chunks())

    # -- TAR extraction -------------------------------------------------- #

    def _extract_tar(self, archive_path, ctx: _ExtractContext) -> None:
        with tarfile.open(archive_path, "r:*") as tf:
            members = [m for m in tf.getmembers() if _selected(m.name, ctx.members)]
            for member in members:
                _resolve_member(ctx.dest_real, member.name, ctx.strip)
                if member.islnk():
                    _resolve_member(ctx.dest_real, member.linkname, ctx.strip)
            total_bytes = sum(m.size for m in members if m.isfile())
            ctx.progress.start(total_bytes, len(members))

            for member in members:
                ctx.progress.item(member.name)
                self._extract_tar_member(tf, member, ctx)
            # TAR's end marker can precede a compression trailer. Consume the
            # remaining decoder stream before publishing any staged payload.
            if isinstance(tf.fileobj, (gzip.GzipFile, bz2.BZ2File, lzma.LZMAFile)):
                while tf.fileobj.read(CHUNK_SIZE):
                    pass

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
        _extract_entry(ctx, _tar_entry(member))

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
        _extract_entry(ctx, _tar_entry(member), hardlink=member.linkname)

    @staticmethod
    def _extract_tar_file(tf, member, target, ctx: _ExtractContext) -> None:
        def chunks():
            src = tf.extractfile(member)
            if src is None:
                raise ArchiveError(f"unreadable archive member: {member.name!r}")
            with src:
                yield from iter(lambda: src.read(CHUNK_SIZE), b'')
        _extract_entry(ctx, _tar_entry(member), chunks=chunks())


# --------------------------------------------------------------------------- #
#  Entry conversion
# --------------------------------------------------------------------------- #

def _zip_entry(info: zipfile.ZipInfo) -> ArchiveEntry:
    mode = info.external_attr >> 16
    return ArchiveEntry(
        name=info.filename,
        size=info.file_size,
        compressed_size=info.compress_size,
        is_dir=info.is_dir() or stat.S_ISDIR(mode),
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
        is_hardlink=member.islnk(),
        link_target=member.linkname or None,
        mode=stat.S_IMODE(member.mode),
        mtime=float(member.mtime),
    )

# --------------------------------------------------------------------------- #
#  Standalone compression streams
# --------------------------------------------------------------------------- #

class StreamBackend:
    """Single-file GZIP, BZIP2, XZ and LZMA-alone streams."""

    name = "native-stream"
    read_formats = {ArchiveFormat.GZIP, ArchiveFormat.BZIP2,
                    ArchiveFormat.XZ, ArchiveFormat.LZMA}
    write_formats = set(read_formats)

    @staticmethod
    def available() -> bool:
        return True

    def can_write(self, fmt: ArchiveFormat) -> bool:
        return fmt in self.write_formats

    def can_read(self, path: Path) -> bool:
        return (_detect_by_magic(path) or _extension_format(path)) in self.read_formats

    @staticmethod
    def validate_sources(sources, symlinks: str) -> None:
        if len(sources) != 1:
            raise ArchiveError("standalone compression requires exactly one regular file; use a TAR variant for multiple files or directories")
        source = Path(sources[0])
        _require_source(source)
        if not source.is_file() or (source.is_symlink() and symlinks != "follow"):
            raise ArchiveError("standalone compression requires one regular file; directories and stored symlinks are unsupported")

    @staticmethod
    def _open(path, fmt: ArchiveFormat, mode: str, level=None):
        if fmt is ArchiveFormat.GZIP:
            return gzip.open(path, mode, compresslevel=9 if level is None else level)
        if fmt is ArchiveFormat.BZIP2:
            return bz2.open(path, mode, compresslevel=9 if level is None else max(1, level))
        container = lzma.FORMAT_ALONE if fmt is ArchiveFormat.LZMA else lzma.FORMAT_XZ
        preset = None
        if "w" in mode:
            preset = 6 if level is None else level
        return lzma.open(path, mode, format=container, preset=preset)

    @staticmethod
    def _name(path: Path) -> str:
        name = path.name
        # Do not honor attacker-controlled GZIP header filenames.
        for suffix in (".gzip", ".bzip2", ".lzma", ".bz2", ".gz", ".xz"):
            if name.lower().endswith(suffix):
                return name[:-len(suffix)] or "decompressed"
        return name + ".out"

    def list_entries(self, path: Path, password=None) -> list[ArchiveEntry]:
        if password is not None:
            raise ArchiveError("standalone compression streams do not support passwords")
        fmt = detect_format(path)
        size = 0
        with self._open(path, fmt, "rb") as src:
            while True:
                buf = src.read(CHUNK_SIZE)
                if not buf:
                    break
                size += len(buf)
        return [ArchiveEntry(self._name(path), size=size, compressed_size=path.stat().st_size)]

    def metadata(self, path: Path, password=None) -> dict:
        if password is not None:
            raise ArchiveError("standalone compression streams do not support passwords")
        if not self.can_read(path):
            raise FormatError(f"not a standalone compression stream: {path.name!r}")
        return {"encrypted": False}

    def create(self, archive_path, items, fmt, compresslevel, symlinks,
               report, progress, password=None) -> None:
        if password is not None:
            raise ArchiveError("standalone compression streams do not support passwords")
        if len(items) != 1 or not items[0][0].is_file() or (items[0][0].is_symlink() and symlinks != "follow"):
            raise ArchiveError("standalone compression requires exactly one regular file")
        source, name = items[0]
        progress.item(name)
        with source.open("rb") as src, self._open(archive_path, fmt, "wb", compresslevel) as dst:
            while True:
                buf = src.read(CHUNK_SIZE)
                if not buf:
                    break
                dst.write(buf)
                progress.chunk(len(buf))
                report.bytes_in += len(buf)
        report.files += 1

    def extract(self, archive_path, ctx: _ExtractContext, password=None) -> None:
        if password is not None:
            raise ArchiveError("standalone compression streams do not support passwords")
        # Unknown decoded size avoids a redundant unbounded preflight decode.
        entry = ArchiveEntry(self._name(archive_path), compressed_size=archive_path.stat().st_size,
                             size_known=False)
        if not _selected(entry.name, ctx.members):
            ctx.progress.start(0, 0)
            return
        _resolve_member(ctx.dest_real, entry.name, ctx.strip)
        ctx.progress.start(entry.size, 1)
        ctx.progress.item(entry.name)
        fmt = detect_format(archive_path)
        with self._open(archive_path, fmt, "rb") as src:
            _extract_entry(ctx, entry, chunks=iter(lambda: src.read(CHUNK_SIZE), b""))


# --------------------------------------------------------------------------- #
#  Common archive lifecycle
# --------------------------------------------------------------------------- #

def _backends() -> list[ArchiveBackend]:
    backends = [NativeBackend(), StreamBackend()]
    try:
        from snug_ext import LibarchiveBackend, SevenZipBackend
    except ImportError:
        return backends
    # py7zr exposes reliable password support and creation for 7z.
    backends.extend((SevenZipBackend(), LibarchiveBackend()))
    return backends


_BACKEND_HINTS = {
    ArchiveFormat.SEVEN_ZIP: "7z support requires the py7zr or libarchive backend (install snug-archives[7z] or snug-archives[extended])",
}


def _backend_error(fmt: ArchiveFormat, *, writing: bool) -> FormatError:
    if writing:
        if fmt is ArchiveFormat.SEVEN_ZIP:
            return FormatError("7z creation requires the py7zr backend (install snug-archives[7z])")
        if fmt in {ArchiveFormat.RAR, ArchiveFormat.RAR5, ArchiveFormat.ZIPX,
                   ArchiveFormat.CAB, ArchiveFormat.ISO, ArchiveFormat.LHA,
                   ArchiveFormat.LZH, ArchiveFormat.RPM, ArchiveFormat.DEB,
                   ArchiveFormat.XAR, ArchiveFormat.WARC}:
            return FormatError(f"{fmt.value} creation is not supported; choose a writable format")
        return FormatError(f"{fmt.value} creation requires an available backend that supports it")
    return FormatError(_BACKEND_HINTS.get(fmt, f"{fmt.value} support requires the libarchive backend (install snug-archives[extended])"))


class ArchiveEngine:
    """Select a backend while preserving atomic creation and shared safety."""

    def __init__(self, backends: Sequence[ArchiveBackend] | None = None) -> None:
        self.backends = list(backends) if backends is not None else _backends()

    def writable_formats(self) -> list[ArchiveFormat]:
        return [fmt for fmt in ArchiveFormat
                if any(backend.available() and backend.can_write(fmt)
                       for backend in self.backends)]

    def _read_backend(self, path: Path, fmt: ArchiveFormat) -> ArchiveBackend:
        candidates = [b for b in self.backends if fmt in b.read_formats and b.available()]
        for backend in candidates:
            if backend.can_read(path):
                return backend
        if candidates:
            raise FormatError(f"the installed backend cannot read this {fmt.value} archive or its compression method")
        raise _backend_error(fmt, writing=False)

    def _write_backend(self, fmt: ArchiveFormat) -> ArchiveBackend:
        for backend in self.backends:
            if backend.available() and backend.can_write(fmt):
                return backend
        raise _backend_error(fmt, writing=True)

    @staticmethod
    def _resolve_format(fmt, archive_path: Path, *, for_write: bool) -> ArchiveFormat:
        if fmt is None:
            return detect_format(archive_path, for_write=for_write)
        return _normalize_format(fmt)

    @staticmethod
    def _archive_path(archive) -> Path:
        path = Path(archive)
        if not path.is_file():
            raise ArchiveError(f"no such archive: {path}")
        return path

    @contextmanager
    def _operation(self):
        try:
            yield
        except ArchiveError:
            raise
        except (OSError, EOFError, RuntimeError,
                zipfile.BadZipFile, tarfile.TarError, lzma.LZMAError, zlib.error) as exc:
            raise ArchiveError(str(exc)) from exc

    def list_entries(self, archive, password=None) -> list[ArchiveEntry]:
        path = self._archive_path(archive)
        with self._operation():
            fmt = detect_format(path)
            return self._read_backend(path, fmt).list_entries(path, password=password)

    def info(self, archive, password=None) -> dict:
        path = self._archive_path(archive)
        with self._operation():
            fmt = detect_format(path)
            backend = self._read_backend(path, fmt)
            entries = backend.list_entries(path, password=password)
            info = {
                "path": str(path), "format": fmt.value, "backend": backend.name,
                "archive_size": path.stat().st_size, "entries": len(entries),
                "files": sum(1 for e in entries if not e.is_dir and not e.is_symlink),
                "directories": sum(1 for e in entries if e.is_dir),
                "symlinks": sum(1 for e in entries if e.is_symlink),
                "uncompressed_size": sum(e.size for e in entries if not e.is_dir),
                "can_extract": True,
                "can_create": any(b.available() and b.can_write(fmt) for b in self.backends),
            }
            # Backends supply only metadata actually established by their APIs.
            info.update({key: value for key, value in backend.metadata(path, password=password).items()
                         if key not in info})
            return info

    @staticmethod
    def _collect_items(sources, *, root, symlinks: str, archive_real: str):
        items = list(_iter_items(sources, root=root, symlinks=symlinks,
                                 archive_real=archive_real))
        total_bytes = sum((p.stat().st_size if symlinks == "follow" and p.is_file()
                           else _file_size(p)) for p, _ in items)
        return items, total_bytes

    def create(self, archive, sources: Iterable[str | os.PathLike[str]], *,
               fmt: ArchiveFormat | str | None = None,
               root: str | os.PathLike[str] | None = None,
               compresslevel: int | None = None, symlinks: str = "store",
               progress: ProgressSink | None = None,
               precollected: tuple[list[tuple[Path, str]], int] | None = None,
               password=None) -> CreateReport:
        if symlinks not in ("store", "follow", "skip"):
            raise ValueError("symlinks must be 'store', 'follow' or 'skip'")
        if compresslevel is not None and not 0 <= compresslevel <= 9:
            raise ValueError("compresslevel must be between 0 and 9")
        progress = progress or NullProgress()
        archive_path = Path(archive)
        fmt = self._resolve_format(fmt, archive_path, for_write=True)
        backend = self._write_backend(fmt)
        sources = list(sources)
        if isinstance(backend, StreamBackend):
            backend.validate_sources(sources, symlinks)
        archive_real = os.path.realpath(archive_path)
        with self._operation():
            if precollected is not None:
                items, total_bytes = precollected
            else:
                items, total_bytes = self._collect_items(
                    sources, root=root, symlinks=symlinks, archive_real=archive_real)
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            report = CreateReport(archive=archive_path, format=fmt)
            progress.start(total_bytes, len(items))
            start = time.monotonic()
            fd, tmp_name = tempfile.mkstemp(prefix=f".{archive_path.name}.", suffix=".part",
                                            dir=str(archive_path.parent))
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                backend.create(tmp_path, items, fmt, compresslevel, symlinks,
                               report, progress, password=password)
                os.replace(tmp_path, archive_path)
            except BaseException:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            report.elapsed = time.monotonic() - start
            report.entries = report.files + report.directories + report.symlinks
            report.bytes_out = archive_path.stat().st_size
            progress.done()
            return report

    def extract(self, archive, destination=".", *,
                members: Sequence[str] | Callable[[str], bool] | None = None,
                overwrite: bool = True, preserve_metadata: bool = True,
                symlinks: str = "store", strip_components: int = 0,
                progress: ProgressSink | None = None, password=None,
                limits: ExtractionLimits | None = None) -> ExtractReport:
        if symlinks not in ("store", "skip"):
            raise ValueError("symlinks must be 'store' or 'skip'")
        if strip_components < 0:
            raise ValueError("strip_components must be >= 0")
        path = self._archive_path(archive)
        progress = progress or NullProgress()
        with self._operation():
            fmt = detect_format(path)
            backend = self._read_backend(path, fmt)
            selected_members = set(members) if isinstance(members, (list, tuple, set, frozenset)) else members
            budget = _ExtractionBudget(limits)
            if isinstance(backend, StreamBackend):
                entries = [ArchiveEntry(backend._name(path), compressed_size=path.stat().st_size,
                                        size_known=False)]
            else:
                entries = backend.list_entries(path, password=password)
            budget.preflight([entry for entry in entries if _selected(entry.name, selected_members)])
            dest = Path(destination)
            dest.mkdir(parents=True, exist_ok=True)
            report = ExtractReport(archive=path, destination=dest)
            ctx = _ExtractContext(
                dest_real=Path(os.path.realpath(dest)),
                members=selected_members, budget=budget,
                overwrite=overwrite, preserve_metadata=preserve_metadata,
                symlinks=symlinks, strip=strip_components, report=report, progress=progress)
            start = time.monotonic()
            try:
                backend.extract(path, ctx, password=password)
                for output in ctx.pending_outputs:
                    output.commit()
                for entry, target, hardlink in ctx.deferred_hardlinks:
                    _commit_hardlink(ctx, entry, target, hardlink)
            finally:
                for output in ctx.pending_outputs:
                    output.abort()
            for directory, mode, mtime in sorted(ctx.deferred_dirs,
                                                  key=lambda item: len(item[0].parts), reverse=True):
                _check_inside(ctx.dest_real, directory)
                if not directory.is_symlink() and directory.is_dir():
                    _apply_metadata(directory, mode, mtime, ctx.preserve_metadata)
            report.elapsed = time.monotonic() - start
            progress.done()
            return report
