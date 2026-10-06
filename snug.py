#!/usr/bin/env python3
"""Snug — a lightweight Python CLI archive manager with live speed and ETA.

Run without arguments for the existing interactive terminal interface, or use
create, extract, list, info and update. Archive I/O and safety live in snug_core;
optional broad-format and 7z implementations live in snug_ext.
"""
from __future__ import annotations

import argparse
import getpass
import os
import queue
import shutil
import sys
import tarfile
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, Sequence

if TYPE_CHECKING:
    from snug_update import AutomaticCheck

from snug_core import (
    ArchiveEngine, ArchiveEntry, ArchiveError, ArchiveFormat, CreateReport,
    ExtractReport, FormatError, NativeBackend, NullProgress, ProgressDisplay,
    ProgressSink, StreamBackend, UnsafeArchiveError, __version__,
    _ARCHIVE_SUFFIXES, _C, _file_size, _init_color, _iter_items, _paint,
    _safe, _truncate, _visible_len, detect_format, human_bytes, human_time,
)


class _QuitInteractive(Exception):
    """The user chose to leave an interactive prompt."""


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
            lines.append("  " + _paint(_truncate(_safe(self.subtitle), cols - 4), _C.DIM))
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
        keys = "/".join(key for key, _ in self.options if len(key) == 1)
        return [
            "",
            "  " + _paint(
                f"↑/↓ move  •  enter select  •  {keys} jump  •  q quit",
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
        if _match_option_key(key, self.options):
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
        return _safe(entry.name) + "/  ▸"
    if entry.is_symlink():
        return _safe(entry.name) + "@"
    return _safe(entry.name)


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

    labels = ["…" if i is None else _safe(current.parts[i]) for i in display_idx]
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
            names = sorted(_safe(p.name) for p in self.marked)
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
        line += "  " + _paint(_truncate(_safe(last_name), 40), _C.DIM)
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
    suffix = f" [{_safe(default)}]" if default else ""
    sys.stdout.write(_SHOW_CURSOR)
    sys.stdout.flush()
    try:
        response = input(f"  {_safe(prompt)}{suffix}: ").strip()
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
    found = []
    for path in candidates:
        if not path.is_file():
            continue
        if path.name.lower().endswith(_ARCHIVE_SUFFIXES):
            found.append(path)
            continue
        try:
            detect_format(path)
        except (ArchiveError, OSError):
            continue
        found.append(path)
    return found


def _build_archive_options(found: list[Path]) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = []
    for i, path in enumerate(found, 1):
        try:
            size = human_bytes(path.stat().st_size)
        except OSError:
            size = "?"
        options.append((str(i), f"{_safe(path.name)}  ({size})"))
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
    print(f"  Not a file: {_safe(str(candidate))}")
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
        print(f"    {_safe(shown)}")
    print()


def _menu_create(engine: ArchiveEngine) -> None:
    cwd = Path.cwd()
    sources = _select_sources_arrow(cwd)
    if not sources:
        _clear_screen()
        print("\n  No sources selected; cancelled.")
        return

    _display_picked_sources(sources, cwd)

    supported = engine.writable_formats()
    print("  Writable formats: " + ", ".join(f.value for f in supported))

    archive_name = _prompt("Archive name", "archive.tar.gz")
    if not archive_name:
        print("  No archive name given; cancelled.")
        return
    archive_name = _ensure_archive_suffix(archive_name)
    archive_path = cwd / archive_name
    # The menu keeps its existing name prompt and offers only installed writers.
    fmt = detect_format(archive_path, for_write=True)
    if fmt not in supported:
        available = ", ".join(f.value for f in supported)
        raise FormatError(f"cannot create {fmt.value}; writable formats: {available}")
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
            print(f"  skipped: {_safe(name)}", file=sys.stderr)


def _menu_extract(engine: ArchiveEngine) -> None:
    archive = _select_archive()
    if archive is None:
        _clear_screen()
        print("\n  Cancelled.")
        return

    _clear_screen()
    print(f"\n  Extracting {_safe(archive.name)} into the current directory…\n")
    display = ProgressDisplay("extract")
    report = engine.extract(archive, ".", progress=display)
    print()
    _print_extract_summary(report)
    if report.skipped:
        for name in report.skipped:
            print(f"  skipped: {_safe(name)}", file=sys.stderr)


def _menu_list(engine: ArchiveEngine) -> None:
    archive = _select_archive()
    if archive is None:
        _clear_screen()
        print("\n  Cancelled.")
        return

    _clear_screen()
    entries = engine.list_entries(archive)
    print(f"\n  {_safe(archive.name)} — {len(entries)} entries\n")
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
        if isinstance(value, bool):
            value = "yes" if value else "no"
        print(f"  {key:<{width}} : {_safe(value)}")
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
        print(f"\n  error: unsafe archive: {_safe(exc)}\n")
    except ArchiveError as exc:
        _clear_screen()
        print(f"\n  error: {_safe(exc)}\n")
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError,
            ValueError) as exc:
        _clear_screen()
        print(f"\n  error: {_safe(exc)}\n")

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

    u = sub.add_parser("update", help="check for releases or update a managed installation")
    update_action = u.add_mutually_exclusive_group()
    update_action.add_argument("--check", action="store_true", help="check for a newer stable release")
    update_action.add_argument("--enable-checks", action="store_true", help="enable daily automatic checks")
    update_action.add_argument("--disable-checks", action="store_true", help="disable automatic checks")

    for command in (c, x, l, i):
        passwords = command.add_mutually_exclusive_group()
        passwords.add_argument("--password", action="store_true",
                               help="securely prompt for a password")
        passwords.add_argument("--password-file", type=Path, metavar="FILE",
                               help="read a UTF-8 password from a file")

    return parser


def _read_password(args) -> str | None:
    if args.password_file is not None:
        # Strip only the final line ending; spaces can be part of a password.
        value = args.password_file.read_text(encoding="utf-8")
        if value.endswith("\n"):
            value = value[:-1]
            if value.endswith("\r"):
                value = value[:-1]
        if "\n" in value or "\r" in value:
            raise ArchiveError("password file must contain exactly one line")
        return value
    if args.password:
        # getpass otherwise falls back to echoed stdin on some platforms.
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                return getpass.getpass("Password: ")
            except (getpass.GetPassWarning, EOFError) as exc:
                raise ArchiveError(
                    "cannot securely prompt here; use --password-file"
                ) from exc
    return None


# -- summary printers ------------------------------------------------------- #

def _print_create_summary(report: CreateReport) -> None:
    ratio = f"{report.ratio * 100:.1f}% of input" if report.bytes_in else "n/a"
    print(f"Created {_safe(report.archive)}  [{report.format.value}]")
    print(f"  entries    : {report.entries} "
          f"({report.files} files, {report.directories} dirs, "
          f"{report.symlinks} links)")
    print(f"  input      : {human_bytes(report.bytes_in)}")
    print(f"  output     : {human_bytes(report.bytes_out)}  ({ratio})")
    print(f"  elapsed    : {human_time(report.elapsed)}")
    print(f"  avg speed  : {human_bytes(report.avg_speed)}/s")


def _print_extract_summary(report: ExtractReport) -> None:
    print(f"Extracted {report.total} items to {_safe(report.destination)}")
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
    when = " " * 16
    if entry.mtime is not None:
        try:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime))
        except (OverflowError, OSError, ValueError):
            pass  # Archive timestamps may exceed the platform's date range.
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
        password=_read_password(args),
    )
    if args.quiet:
        return
    _print_create_summary(report)
    for name in report.skipped:
        print(f"  skipped: {_safe(name)}", file=sys.stderr)


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
        password=_read_password(args),
    )
    if args.quiet:
        return
    _print_extract_summary(report)
    for name in report.skipped:
        print(f"  skipped: {_safe(name)}", file=sys.stderr)


def _cmd_list(args, engine: ArchiveEngine) -> None:
    for entry in engine.list_entries(args.archive, password=_read_password(args)):
        if args.verbose:
            _print_entry_verbose(entry)
        else:
            print(_safe(entry.name))


def _cmd_info(args, engine: ArchiveEngine) -> None:
    data = engine.info(args.archive, password=_read_password(args))
    width = max(len(k) for k in data)
    for key, value in data.items():
        if key in ("archive_size", "uncompressed_size"):
            value = human_bytes(value)
        if isinstance(value, bool):
            value = "yes" if value else "no"
        print(f"{key:<{width}} : {_safe(value)}")


def _cmd_update(args) -> int:
    from snug_update import UpdateError, check_update, perform_update, set_checks
    try:
        if args.enable_checks or args.disable_checks:
            enabled = args.enable_checks
            set_checks(enabled)
            print(f"Automatic update checks {'enabled' if enabled else 'disabled'}.")
        elif args.check:
            result = check_update(__version__)
            if result.available:
                print(f"Current version: {result.current_version}\nLatest version:  {result.latest_version}")
                print("\nUpdate available.\nRun `snug update` to install it.")
            else:
                print(f"Snug {__version__} is up to date.")
        else:
            print(perform_update(__version__))
    except UpdateError as exc:
        print(f"error: {_safe(exc)}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 0


def _start_update_check() -> AutomaticCheck | None:
    try:
        from snug_update import start_automatic_check
        return start_automatic_check(__version__)
    except Exception:
        # Optional discovery cannot change an archive operation's exit status.
        return None


def _update_notice(handle: AutomaticCheck | None) -> None:
    try:
        if sys.stderr.isatty():
            from snug_update import automatic_notice
            notice = automatic_notice(handle)
            if notice:
                print(notice, file=sys.stderr)
    except Exception:
        return


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)

    if not argv:
        handle = _start_update_check() if sys.stdin.isatty() and sys.stdout.isatty() else None
        code = _launch_interactive()
        if code == 0:
            _update_notice(handle)
        return code

    args = _build_parser().parse_args(argv)
    if args.command == "update":
        return _cmd_update(args)

    handle = _start_update_check()
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
        print(f"error: unsafe archive: {_safe(exc)}", file=sys.stderr)
        return 3
    except ArchiveError as exc:
        print(f"error: {_safe(exc)}", file=sys.stderr)
        return 2
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError,
            ValueError) as exc:
        print(f"error: {_safe(exc)}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    if args.command in {"create", "extract"} and not args.quiet:
        _update_notice(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
