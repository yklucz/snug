#!/usr/bin/env python3
"""Snug — a lightweight Python CLI archive manager with live speed and ETA.

Run without arguments for the existing interactive terminal interface, or use
create, extract, list, info, test, doctor, formats and update. Archive I/O and safety live in snug_core;
optional broad-format and 7z implementations live in snug_ext.
"""
from __future__ import annotations

import argparse
import codecs
import getpass
import io
import os
import queue
import re
import shutil
import signal
import sys
import tarfile
import threading
import time
import unicodedata
import zipfile
from collections import deque
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Protocol, Sequence, TypeVar

if TYPE_CHECKING:
    from snug_update import AutomaticCheck

from snug_core import (
    ArchiveEngine, ArchiveEntry, ArchiveError, ArchiveFormat, CreateError, CreateReport, CreateSkip,
    ExtractReport, TestReport, ExtractionLimits, ResourceLimitError, parse_size,
    FormatError, NativeBackend, NullProgress, ProgressDisplay,
    ProgressSink, StreamBackend, UnsafeArchiveError, __version__,
    _ARCHIVE_SUFFIXES, _C, _TTY_REFRESH, _cell_width, _file_size, _init_color, _iter_items, _paint,
    _safe, _truncate, _truncate_cells, _visible_len, detect_format, human_bytes, human_time,
)


_CANCELLED_MESSAGE = "\n  Cancelled."
_INTERRUPTED_MESSAGE = "\ninterrupted"


class _QuitInteractive(Exception):
    """The user chose to leave an interactive prompt."""


class _CancelPrompt(Exception):
    """Return from one line prompt without changing its caller's value."""


class _PromptAction(Exception):
    """An optional editor action, carrying the untouched input buffer."""

    def __init__(self, action: str, text: str) -> None:
        super().__init__(action)
        self.action = action
        self.text = text


# Raw EOF must bypass ordinary exception handlers and retain interruption semantics.
class _TerminalEOF(KeyboardInterrupt):  # NOSONAR(S5709)
    """Distinguish raw-reader EOF while preserving screen interruption rules."""


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


def _draw_current_screen(screen: Screen, size: tuple[int, int], small: bool) -> None:
    if small:
        _draw_lines(_too_small_lines(), size=size)
    else:
        cols, rows = size
        _draw_lines(screen.draw(cols, rows), footer=screen.footer(), size=size)


def _read_screen_key(screen: Screen) -> str | None:
    try:
        return _read_key_timeout(0.1)
    except _TerminalEOF:
        raise
    except KeyboardInterrupt:
        if not getattr(screen, "cancel_on_interrupt", False):
            raise
        return "esc"


def _run_screen(initial: Screen) -> None:
    """Own raw mode, redraw, and key reading for one screen loop.

    A handler returns ``None`` to stay on the current screen, a new
    ``Screen`` to transition, or ``_EXIT`` to terminate the loop.
    """
    screen: Screen = initial
    drawn_size: tuple[int, int] | None = None
    redraw = True
    with _raw_mode():
        while True:
            cols, rows = _term_size()
            size = (cols, rows)
            small = _size_mode(cols, rows) == "small"
            if redraw or size != drawn_size:
                _draw_current_screen(screen, size, small)
                drawn_size = size
                redraw = False
            key = _read_screen_key(screen)
            if key is None:
                continue
            # A resize may arrive during the input poll; gate the key against
            # the current size before a handler can start an operation.
            if _size_mode(*_term_size()) == "small" and key != "esc":
                continue
            nxt = screen.handle(key)
            if nxt is _EXIT:
                return
            if nxt is not None and not isinstance(nxt, _ExitMarker):
                screen = nxt
            redraw = True


def _cursor_at(row: int, col: int = 1, *, size: tuple[int, int] | None = None) -> str:
    cols, rows = size if size is not None else _term_size()
    row = max(1, min(row, max(1, rows)))
    col = max(1, min(col, max(1, cols)))
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
    _TUI_LAST_LINES.clear()
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
def _raw_mode(*, when: int | None = None):
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
        if when is None:
            tty.setraw(fd)
        else:
            tty.setraw(fd, when)
        yield old
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if sys.platform == "darwin" and not old[3] & termios.PENDIN:
            # Settle macOS raw -> canonical PENDIN without reading input.
            import select
            select.select([fd], [], [], 0)


# -- key reader ------------------------------------------------------------- #

def _read_key() -> str:
    """Return one of: up, down, left, right, enter, esc, space, backspace,
    home/end, page_up/down, delete, ctrl_a/e/d/k/u/w, confirm (Tab), quit, or a character."""
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
    if ch == b"\t":
        return "confirm"
    if ch in (b"\xe0", b"\x00"):  # extended key prefix
        return _decode_extended_windows(getch)
    return _decode_simple_char(ch)


def _decode_extended_windows(getch) -> str:
    ch2 = getch()
    paging = {b"I": "page_up", b"Q": "page_down", b"G": "home", b"O": "end"}
    if ch2 in paging:
        return paging[ch2]
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

_UNIX_KEY_PUSHBACK: deque[bytes] = deque()


def _read_unix_byte(fd: int) -> bytes:
    if _UNIX_KEY_PUSHBACK:
        return _UNIX_KEY_PUSHBACK.popleft()
    return os.read(fd, 1)


def _unix_byte_ready(fd: int, timeout: float) -> bool:
    if _UNIX_KEY_PUSHBACK:
        return True
    import select

    return bool(select.select([fd], [], [], timeout)[0])


def _utf8_key_length(value: int) -> int:
    if 0xC2 <= value <= 0xDF:
        return 2
    if 0xE0 <= value <= 0xEF:
        return 3
    if 0xF0 <= value <= 0xF4:
        return 4
    return 0


def _read_utf8_continuation(fd: int) -> bytes | None:
    if not _unix_byte_ready(fd, 0.05):
        return None
    continuation = _read_unix_byte(fd)
    if not continuation:
        return None
    if continuation == b"\x03":
        raise KeyboardInterrupt
    if not 0x80 <= continuation[0] <= 0xBF:
        _UNIX_KEY_PUSHBACK.appendleft(continuation)
        return None
    return continuation


def _read_utf8_key(fd: int, lead: bytes) -> str:
    """Read one UTF-8 character, with bounded waits for its continuation bytes."""
    length = _utf8_key_length(lead[0])
    if not length:
        return "other"

    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        decoder.decode(lead)
        character = ""
        for index in range(length - 1):
            continuation = _read_utf8_continuation(fd)
            if continuation is None:
                return "other"
            character = decoder.decode(continuation, final=index == length - 2)
    except UnicodeDecodeError:
        return "other"
    return character if character.isprintable() or character in ("\u200c", "\u200d") else "other"


def _read_key_unix() -> str:
    fd = sys.stdin.fileno()
    b = _read_unix_byte(fd)
    if not b:
        raise _TerminalEOF
    if b == b"\x03":
        raise KeyboardInterrupt
    if b == b"\t":
        return "confirm"
    if b == b"\x1b":
        return _read_arrow_sequence(fd)
    controls = {
        b"\x01": "ctrl_a", b"\x05": "ctrl_e", b"\x04": "ctrl_d",
        b"\x0b": "ctrl_k", b"\x15": "ctrl_u", b"\x17": "ctrl_w",
    }
    if b in controls:
        return controls[b]
    if b[0] >= 0x80:
        return _read_utf8_key(fd, b)
    return _decode_simple_char(b)


def _decode_arrow_final(introducer: bytes, parameters: bytearray, final: bytes) -> str:
    if final == b"~" and introducer == b"[":
        return {b"1": "home", b"7": "home", b"4": "end", b"8": "end",
                b"3": "delete", b"5": "page_up", b"6": "page_down"}.get(
                    bytes(parameters).split(b";")[0], "other")
    return {
        b"A": "up",
        b"B": "down",
        b"C": "right",
        b"D": "left",
        b"H": "home",
        b"F": "end",
    }.get(final, "other")


def _read_arrow_sequence(fd: int) -> str:
    """Decode an ANSI escape sequence and consume it completely."""
    if not _unix_byte_ready(fd, 0.05):
        return "esc"
    introducer = _read_unix_byte(fd)
    if introducer not in (b"[", b"O"):
        if introducer:
            _UNIX_KEY_PUSHBACK.appendleft(introducer)
        return "esc"

    parameters = bytearray()
    while True:
        if not _unix_byte_ready(fd, 0.05):
            return "other"
        b = _read_unix_byte(fd)
        if not b:
            raise _TerminalEOF
        if b == b"\x03":
            raise KeyboardInterrupt
        if 0x40 <= b[0] <= 0x7E:
            return _decode_arrow_final(introducer, parameters, b)
        parameters.extend(b)


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

_TUI_SGR = re.compile(r"\x1b\[[0-9;]*m")
_TUI_LAST_LINES: list[str] = []


def _tui_char_width(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) in (
        "Mn", "Me", "Cf", "Cc", "Cs",
    ):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _tui_cell_width(text: str) -> int:
    """Terminal cells for TUI text; leave the shared CLI helpers unchanged."""
    return sum(_tui_char_width(char) for char in _TUI_SGR.sub("", text))


def _tui_truncate(text: str, width: int) -> str:
    """Keep whole characters and SGR styling within a terminal-cell budget."""
    if width <= 0:
        return ""
    if _tui_cell_width(text) <= width:
        return text
    budget = width - 1  # reserve one cell for the ellipsis
    cells = 0
    pos = 0
    kept: list[str] = []
    styled = False
    while pos < len(text):
        sgr = _TUI_SGR.match(text, pos)
        if sgr is not None:
            kept.append(sgr.group())
            styled = True
            pos = sgr.end()
            continue
        char = text[pos]
        char_width = _tui_char_width(char)
        if cells + char_width > budget:
            break
        kept.append(char)
        cells += char_width
        pos += 1
    return "".join(kept) + "…" + (_C.RESET if styled else "")


def _size_mode(cols: int, rows: int) -> str:
    if cols < 40 or rows < 10:
        return "small"
    return "full" if cols >= 80 and rows >= 24 else "compact"


def _too_small_lines() -> list[str]:
    return ["Terminal too small", "Minimum: 40x10", "Esc: back · Ctrl+C: cancel/quit"]


def _term_size() -> tuple[int, int]:
    size = shutil.get_terminal_size((100, 30))
    return max(1, size.columns), max(1, size.lines)


def _draw_lines(lines: list[str], footer: list[str] | None = None, *,
                size: tuple[int, int] | None = None) -> None:
    """Budget body and footer together, without bottom-row newlines."""
    cols, rows = size if size is not None else _term_size()
    cols, rows = max(1, cols), max(1, rows)
    size = (cols, rows)
    footer = (footer or [])[-rows:]
    body = lines[:rows - len(footer)]
    _TUI_LAST_LINES[:] = lines
    out = sys.stdout
    out.write(_HOME + _CLEAR_BELOW)
    for row, line in enumerate(body, 1):
        out.write(_cursor_at(row, size=size) + _C.RESET
                  + _tui_truncate(line, max(1, cols - 1)) + _CLEAR_LINE)
    start_row = rows - len(footer) + 1
    for i, line in enumerate(footer):
        out.write(_cursor_at(start_row + i, size=size) + _C.RESET + _CLEAR_LINE
                  + _tui_truncate(line, max(1, cols - 1)))
    out.flush()


def _draw_footer(lines: list[str]) -> None:
    """Redraw just the footer rows at the bottom of the screen."""
    cols, rows = _term_size()
    if _size_mode(cols, rows) == "small":
        _draw_lines(_too_small_lines(), size=(cols, rows))
        return
    lines = lines[-rows:]
    out = sys.stdout
    start_row = rows - len(lines) + 1
    for i, line in enumerate(lines):
        out.write(_cursor_at(start_row + i, size=(cols, rows)) + _C.RESET + _CLEAR_LINE
                  + _tui_truncate(line, max(1, cols - 1)))
    out.flush()


# -- generic option menu ---------------------------------------------------- #

def _match_option_key(key: str, options: list[tuple[str, str]]) -> bool:
    return any(key_str == key for key_str, _ in options)


class _MenuScreen:
    """Full-screen option menu."""

    cancel_on_interrupt = True

    _PINNED_ACTIONS = frozenset((
        "Back", "Quit", "Create archive", "Extract archive", "Test archive",
        "Use this selection", "Use these limits",
    ))

    def __init__(self, title: str, subtitle: str | None,
                 options: list[tuple[str, str]]) -> None:
        self.title = title
        self.subtitle = subtitle
        self.options = options
        self.selected = 0
        self.result: str | None = None
        self._mode = "full"

    def draw(self, cols: int, rows: int) -> list[str]:
        self._mode = _size_mode(cols, rows)
        if self._mode == "small":
            return _too_small_lines()
        if self._mode == "compact":
            title = self.title
            if self.subtitle:
                title += " · " + _safe(self.subtitle)
            lines = [_paint(title, _C.BOLD)]
            room = rows - len(lines) - len(self.footer())
            indices = self._compact_indices(room)
            lines.extend(self._option_line(i) for i in indices)
            return lines
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
            lines.append("  " + _paint(_tui_truncate(_safe(self.subtitle), cols - 4), _C.DIM))
            lines.append("")
        lines.append("  " + _paint(self.title, _C.BOLD))
        lines.append("")

        room = rows - len(lines) - len(self.footer())
        start, end = _visible_range(len(self.options), self.selected, room)
        lines.extend(self._option_line(i) for i in range(start, end))
        return lines

    def _compact_indices(self, room: int) -> list[int]:
        if len(self.options) <= room:
            return list(range(len(self.options)))
        pinned = [i for i, (_, label) in enumerate(self.options)
                  if label in self._PINNED_ACTIONS]
        scrolling = [i for i in range(len(self.options)) if i not in pinned]
        cursor = scrolling.index(self.selected) if self.selected in scrolling else 0
        start, end = _visible_range(len(scrolling), cursor, room - len(pinned))
        return scrolling[start:end] + pinned

    def _option_line(self, i: int) -> str:
        key, label = self.options[i]
        if i == self.selected:
            return ("  " + _paint("❯", _C.BOLD_GREEN)
                    + _paint(f" {key})  {label}", _C.BOLD_WHITE))
        return "  " + f"  {key})  {label}"

    def footer(self) -> list[str]:
        if self._mode == "small":
            return []
        if self._mode == "compact":
            return [_paint("↑↓ Enter · Esc/q/Q/Ctrl+C back" if self.cancel_on_interrupt
                           else "↑↓ Enter · Esc/q/Q quit · Ctrl+C 130", _C.DIM)]
        return [
            "  " + _paint("↑/↓ move · Enter select · single-key shortcut activates", _C.DIM),
            "  " + _paint("Esc/q/Q/Ctrl+C back" if self.cancel_on_interrupt
                         else "Esc/q/Q/0 quit · Ctrl+C quits with 130", _C.DIM),
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
                 subtitle: str | None = None, *, cancel_on_interrupt: bool = True) -> str | None:
    """Display a full-screen arrow-navigable menu."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _select_menu_plain(title, options)
    screen = _MenuScreen(title=title, subtitle=subtitle, options=options)
    screen.cancel_on_interrupt = cancel_on_interrupt
    _run_screen(screen)
    return screen.result


def _tui_clusters(text: str) -> list[str]:
    """Keep zero-width marks attached to their preceding visible character."""
    clusters: list[str] = []
    for char in text:
        if clusters and not _tui_char_width(char):
            clusters[-1] += char
        else:
            clusters.append(char)
    return clusters


def _tui_middle(text: str, width: int) -> str:
    """Shorten a path in the middle, giving its filename the larger budget."""
    if _tui_cell_width(text) <= width:
        return text
    if width <= 1:
        return "…" if width else ""
    clusters = _tui_clusters(text)
    head_budget = (width - 1) // 3
    head: list[str] = []
    while clusters and _tui_cell_width("".join(head) + clusters[0]) <= head_budget:
        head.append(clusters.pop(0))
    tail: list[str] = []
    tail_budget = width - 1 - _tui_cell_width("".join(head))
    while clusters and _tui_cell_width(clusters[-1] + "".join(tail)) <= tail_budget:
        tail.insert(0, clusters.pop())
    return "".join(head) + "…" + "".join(tail)


def _tui_wrap_word(word: str, width: int) -> list[str]:
    pieces = [""]
    for cluster in _tui_clusters(word):
        if pieces[-1] and _tui_cell_width(pieces[-1] + cluster) > width:
            pieces.append("")
        pieces[-1] += cluster
    return pieces


def _tui_wrap(text: str, width: int) -> list[str]:
    """Wrap messages without dropping text or splitting combining marks."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        if current and _tui_cell_width(current + " " + word) <= width:
            current += " " + word
            continue
        if current:
            lines.append(current)
        pieces = _tui_wrap_word(word, width)
        lines.extend(pieces[:-1])
        current = pieces[-1]
    return lines + [current]


_TUI_QUOTED_PATH = re.compile(
    r"(['\"])((?![A-Za-z][A-Za-z0-9+.-]*://)[^'\"]*[\\/][^'\"]*)\1")
_TUI_BARE_PATH = re.compile(r"(?<!\S)(?:/|[A-Za-z]:[\\/])[^\s'\"]+")


def _tui_pause_paths(line: str, width: int) -> str:
    """Recognize result paths and quoted filesystem diagnostics, not causes."""
    budget = max(1, width - 10)
    if line.startswith("warning: skipped "):
        name, separator, cause = line[len("warning: skipped "):].partition(": ")
        return "warning: skipped " + _tui_middle(name, budget) + separator + cause
    if line.startswith(("Created ", "Tested ")) and "  [" in line:
        prefix, path = line.split(" ", 1)
        path, suffix = path.rsplit("  [", 1)
        return prefix + " " + _tui_middle(path, budget) + "  [" + suffix
    if line.startswith("Extracted ") and " items to " in line:
        prefix, path = line.split(" items to ", 1)
        return prefix + " items to " + _tui_middle(path, budget)
    if line.startswith(("Output: ", "Archive: ", "skipped: ")):
        prefix, path = line.split(": ", 1)
        return prefix + ": " + _tui_middle(path, budget)
    line = _TUI_QUOTED_PATH.sub(
        lambda match: match[1] + _tui_middle(match[2], budget) + match[1], line)
    return _TUI_BARE_PATH.sub(lambda match: _tui_middle(match[0], budget), line)


def _tui_pause_lines(lines: list[str], width: int) -> list[str]:
    plain = [_TUI_SGR.sub("", line).strip() for line in lines]
    while plain and not plain[0]:
        plain.pop(0)
    while plain and not plain[-1]:
        plain.pop()
    return [row for line in plain for row in _tui_wrap(_tui_pause_paths(line, width), width)]


class _PauseScreen:
    """Scrollable, wrapped result/error output with a reserved overflow row."""

    cancel_on_interrupt = True

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self._mode = "full"
        self._offset = 0
        self._page = 1
        self._wrapped: list[str] = []

    def draw(self, cols: int, rows: int) -> list[str]:
        self._mode = _size_mode(cols, rows)
        if self._mode == "small":
            return _too_small_lines()
        self._wrapped = _tui_pause_lines(self.lines, cols - 1)
        room = rows - len(self.footer())
        overflow = len(self._wrapped) > room
        self._page = max(1, room - int(overflow))
        self._offset = min(self._offset, max(0, len(self._wrapped) - self._page))
        end = self._offset + self._page
        body = self._wrapped[self._offset:end]
        if overflow:
            hidden = len(self._wrapped) - len(body)
            body.append(f"{hidden} hidden · lines {self._offset + 1}-{end} of {len(self._wrapped)}")
        return body

    def footer(self) -> list[str]:
        if self._mode == "small":
            return []
        if self._mode == "compact":
            return [_paint("↑↓ PgUp/PgDn Home/End Enter/Esc/Ctrl+C", _C.DIM)]
        return [_paint("↑/↓ scroll · PgUp/PgDn page · Home/End first/last", _C.DIM),
                _paint("Enter/Esc/Ctrl+C Continue", _C.DIM)]

    def handle(self, key: str) -> _ExitMarker | None:
        if key in ("enter", "esc"):
            return _EXIT
        moves = {"up": -1, "down": 1, "page_up": -self._page, "page_down": self._page,
                 "home": -len(self._wrapped), "end": len(self._wrapped)}
        self._offset = max(0, min(self._offset + moves.get(key, 0),
                                  max(0, len(self._wrapped) - self._page)))
        return None


@contextmanager
def _menu_result_output():
    """Collect only TUI result printing; shared CLI printers stay unchanged."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        yield
        return
    output = io.StringIO()
    completed = False
    with redirect_stdout(output), redirect_stderr(output):
        try:
            yield
            completed = True
        finally:
            if completed:
                _TUI_LAST_LINES[:] = output.getvalue().splitlines()


def _wait_for_enter() -> None:
    """Scroll a bounded result, including on idle resize, until dismissal."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    _run_screen(_PauseScreen(list(_TUI_LAST_LINES)))


# =========================================================================== #
#  File picker (arrow-key source selection)
# =========================================================================== #

def _path_probe(check: Callable[[], bool]) -> tuple[bool, OSError | None]:
    """Keep input classification failures separate from a usable path."""
    try:
        return check(), None
    except OSError as exc:
        return False, exc


def _path_usable(check: Callable[[], bool]) -> bool:
    return _path_probe(check)[0]


def _is_enterable(path: Path) -> bool:
    """True for directories we can navigate into (not symlinks to dirs)."""
    return _path_usable(lambda: path.is_dir() and not path.is_symlink())


def _list_dir_entries(path: Path) -> list[Path]:
    """Return entries sorted: subdirectories first, then files and symlinks."""
    items = list(path.iterdir())
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
    if _path_usable(entry.is_symlink):
        return _safe(entry.name) + "@"
    return _safe(entry.name)


def _picker_help_line() -> str:
    return "Enter/→ open dir · Space mark · Tab use marks · Enter file: marks or file"


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
    return sum(_tui_cell_width(s) for s in labels) + 3 * max(0, len(labels) - 1)


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

    def __init__(self, origin: Path, *, single: bool = False) -> None:
        self.origin = origin
        self.single = single
        self.current = origin
        self.entries: list[Path] = []
        self._directories: set[Path] = set()
        self.cursor = 0
        self.marked: set[Path] = set()
        self.filter = ""
        self.filter_mode = False
        self.result: list[Path] | None = None
        self._row_count = 12
        self._mode = "full"
        self._read_error: str | None = None
        self._notice: str | None = None
        self._load_entries()

    # -- helpers -------------------------------------------------------- #

    def _visible(self) -> list[Path]:
        if not self.filter:
            return self.entries
        needle = unicodedata.normalize("NFC", self.filter).lower()
        return [p for p in self.entries
                if needle in unicodedata.normalize("NFC", p.name).lower()]

    def _load_entries(self) -> None:
        """Keep a failed directory read distinct from an empty directory."""
        try:
            self.entries = _list_dir_entries(self.current)
            self._directories = {path for path in self.entries if _is_enterable(path)}
        except OSError as exc:
            self.entries = []
            self._directories = set()
            self._read_error = _safe(exc.strerror or str(exc))
        else:
            self._read_error = None
        self._notice = None

    def _marked_line(self) -> str:
        if self.marked:
            names = sorted(_safe(p.name) for p in self.marked)
            text = f"  Marked ({len(self.marked)}): " + ", ".join(names)
            return _paint(text, _C.BOLD_YELLOW)
        return _paint(
            "  Marked (0): Space marks; Tab confirms marked sources", _C.DIM
        )

    # -- Screen protocol ------------------------------------------------ #

    def draw(self, cols: int, rows: int) -> list[str]:
        self._mode = _size_mode(cols, rows)
        if self._mode == "small":
            return _too_small_lines()
        lines = self._header_lines(cols)
        if self._notice:
            lines.append("  " + _paint(self._notice, _C.BOLD_YELLOW))
        self._row_count = rows - len(lines) - len(self.footer())
        visible = self._visible()
        self._clamp_cursor(len(visible))

        if self._read_error is not None:
            lines.extend(self._render_read_error())
        elif visible:
            lines.extend(self._render_entries(visible))
        else:
            lines.extend(self._render_empty())
        return lines

    def _clamp_cursor(self, total: int) -> None:
        self.cursor = max(0, min(self.cursor, total - 1)) if total else 0

    def _header_lines(self, cols: int) -> list[str]:
        if self._mode == "compact":
            location = f"Filter: {self.filter}" if self.filter else _safe(self.current)
            if self.single:
                return [_paint("↵ Open/OK · ", _C.BOLD) + location]
            return [_paint(f"Marked ({len(self.marked)}) · ↵ Open/OK · ", _C.BOLD)
                    + location]
        lines = [
            "",
            "  " + _paint("Select an archive" if self.single else "Select sources to archive", _C.BOLD),
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

    def _render_read_error(self) -> list[str]:
        return ["  " + _paint(f"Error: {self._read_error}", _C.BOLD_RED),
                "  Enter Retry · ← Back"] + [""] * (self._row_count - 2)

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
        elif _path_usable(entry.is_symlink):
            name = _paint(name, _C.CYAN)
        if self.single:
            return f"  {cursor_marker} {name}"
        return f"  {cursor_marker} {mark_marker} {name}"

    def footer(self) -> list[str]:
        if self._mode == "small":
            return []
        if self.single:
            if self._mode == "compact":
                if self.filter or self.filter_mode:
                    return [_paint("↑↓ ←→ Tab OK · Esc Clear · Ctrl+C Back", _C.DIM)]
                return [_paint("↑↓ ←→ Tab OK · Esc/Ctrl+C Back / Filter", _C.DIM)]
            return [
                "  " + _paint("Enter/→ open dir · Enter/Tab choose file", _C.DIM),
                "  " + _paint("↑↓ move · ← back · Type or / filter · Backspace erase filter", _C.DIM),
                "  " + _paint("Esc clears filter/cancels · Ctrl+C returns to path · q/Q cancel unfiltered", _C.DIM),
            ]
        if self._mode == "compact":
            help_line = ("Space Mark Tab OK Esc Clear Ctrl+C Back" if self.filter or self.filter_mode
                         else "Space Mark Tab OK Esc/Ctrl+C Back")
            return [_paint(help_line, _C.DIM)]
        return [
            "  " + _paint(_picker_help_line(), _C.DIM),
            "  " + _paint("↑↓ · ← up · Type or / filter · Esc clear/back · Ctrl+C back · q/Q unfiltered", _C.DIM),
            self._marked_line(),
        ]

    def handle(self, key: str) -> "Screen | _ExitMarker | None":
        if key in ("esc", "backspace", "space"):
            return self._handle_filter_or_action(key)
        if key == "confirm":
            return _EXIT if self._confirm() else None
        if key == "enter":
            if self._read_error is not None:
                self._load_entries()
                self._clamp_cursor(len(self._visible()))
                return None
            visible = self._visible()
            if visible and self._can_enter(visible[self.cursor]):
                self._enter()
                return None
            return _EXIT if self._confirm(highlighted=True) else None
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
        if not self.single:
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
        if not self._can_enter(target):
            return
        self.current = target
        self._load_entries()
        self.cursor = 0
        self.filter = ""
        self.filter_mode = False

    def _can_enter(self, target: Path) -> bool:
        # A listed directory can disappear before the key arrives. Attempt its
        # read so the failure is visible, while still refusing live symlinks.
        return _path_usable(lambda: (target.is_dir() and not target.is_symlink())
                or (target in self._directories and not target.exists()
                    and not target.is_symlink()))

    def _go_up(self) -> None:
        if self.current == self.current.parent or (not self.single and self.current == self.origin):
            return
        previous = self.current
        parent = previous.parent
        self.current = parent
        self._load_entries()
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
        self._notice = None

    def _confirm(self, *, highlighted: bool = False) -> bool:
        if self.single:
            visible = self._visible()
            if not visible:
                self._notice = "No file highlighted."
                return False
            target = visible[self.cursor]
            return self._confirm_single_target(target)
        if self.marked:
            self.result = sorted(self.marked, key=str)
            return True
        if highlighted:
            # Preserve the existing file (and empty-list) Enter contract.
            visible = self._visible()
            self.result = [visible[self.cursor]] if visible else None
            return True
        self._notice = "Nothing marked. Space marks; Tab OK."
        return False

    def _confirm_single_target(self, target: Path) -> bool:
        directory, error = _path_probe(target.is_dir)
        symlink, link_error = _path_probe(target.is_symlink)
        error = error or link_error
        if error is not None:
            self._notice = _cli_os_reason(error, output=False)
            return False
        if directory:
            self._notice = ("Symlinked directories stay closed." if symlink
                            else "Choose a file; Enter/→ opens dirs.")
            return False
        self.result = [target]
        return True

    def _cancel(self) -> None:
        self.result = None


def _select_sources_arrow(origin: Path, *, single: bool = False) -> list[Path] | None:
    """Shared arrow-key picker, restricted to *origin* in multi-select mode.

    Returns selected paths (one in single mode), or ``None`` on cancellation.
    Single mode can go up beyond *origin* and remembers its last directory.
    """
    global _ARCHIVE_BROWSE_DIRECTORY
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    screen = _PickerScreen(origin, single=single)
    try:
        _run_screen(screen)
    except KeyboardInterrupt:
        return None
    finally:
        if single:
            _ARCHIVE_BROWSE_DIRECTORY = screen.current
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
    fd = sys.stdin.fileno()
    if not _unix_byte_ready(fd, timeout):
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
        line += "  " + _paint(_tui_truncate(_safe(last_name), 40), _C.DIM)
    cols = shutil.get_terminal_size((120, 24)).columns
    if _tui_cell_width(line) > cols - 1:
        line = _tui_truncate(line, cols - 1)
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
    skip_errors: list[CreateSkip] | None = None,
) -> None:
    """Run in a worker thread; feed ``(kind, ...)`` messages into ``q``."""
    try:
        items: list[tuple[Path, str]] = []
        total = 0
        skip_errors = [] if skip_errors is None else skip_errors
        for fs_path, name in _iter_items(
            sources, root=root, symlinks=symlinks,
            archive_real=archive_real, cancel=cancel, skip_errors=skip_errors,
        ):
            items.append((fs_path, name))
            total += _file_size(fs_path)
            q.put(("item", len(items), total, name))
        if cancel.is_set():
            q.put(("cancelled",))
        else:
            box[0] = (items, total, skip_errors) if skip_errors else (items, total)
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


def _interactive_enumerate(sources, *, root, symlinks, archive_real,
                           skip_errors: list[CreateSkip] | None = None):
    """Walk the tree in a worker thread, showing a spinner.

    Returns collected items, byte total, and any skip diagnostics, or
    ``None`` if the user cancelled with Esc, q, or Ctrl+C.
    """
    skip_errors = [] if skip_errors is None else skip_errors
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        items, total = ArchiveEngine._collect_items(
            sources, root=root, symlinks=symlinks, archive_real=archive_real,
            skip_errors=skip_errors,
        )
        return (items, total, skip_errors) if skip_errors else (items, total)

    q: "queue.Queue[tuple]" = queue.Queue()
    cancel = threading.Event()
    box: list = [None]

    t = threading.Thread(
        target=_enumerate_worker,
        args=(sources, root, symlinks, archive_real, q, cancel, box, skip_errors),
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

def _edit_boundaries(text: str) -> list[int]:
    """Group each base with its following zero-cell marks for editing."""
    return [0] + [index for index, char in enumerate(text)
                  if index and _tui_char_width(char)] + ([len(text)] if text else [])


@dataclass
class _LineEditor:
    """Pure line-editing state; indices refer to the original Unicode text."""

    initial_text: str = ""
    history: Sequence[str] = ()
    extra_keys: Mapping[str, str] = field(default_factory=dict)
    buffer: str = field(init=False)
    cursor: int = field(init=False)
    history_position: int = field(init=False)
    _draft: tuple[str, int] = field(init=False)

    def __post_init__(self) -> None:
        self.history = tuple(self.history)
        self.buffer = self.initial_text
        self.cursor = len(self.buffer)
        self.history_position = len(self.history)
        self._draft = (self.buffer, self.cursor)

    def handle(self, key: str) -> str | None:
        if key in ("enter", "esc"):
            return "submit" if key == "enter" else "cancel"
        if key == "ctrl_d" and not self.buffer:
            return "eof"
        if key in self.extra_keys:
            return self.extra_keys[key]
        boundaries = _edit_boundaries(self.buffer)
        previous = max((pos for pos in boundaries if pos < self.cursor), default=0)
        following = min((pos for pos in boundaries if pos > self.cursor), default=len(self.buffer))
        if self._edit_key(key, boundaries, previous, following):
            return None
        if key in ("up", "down"):
            self._recall_history(key)
        else:
            self._insert_text(key)
        return None

    def _edit_key(self, key: str, boundaries: list[int], previous: int,
                  following: int) -> bool:
        positions = {"home": 0, "ctrl_a": 0, "end": len(self.buffer),
                     "ctrl_e": len(self.buffer), "left": previous, "right": following}
        if key in positions:
            self.cursor = positions[key]
        elif key == "backspace":
            self.buffer = self.buffer[:previous] + self.buffer[self.cursor:]
            self.cursor = previous
        elif key in ("delete", "ctrl_d"):
            self.buffer = self.buffer[:self.cursor] + self.buffer[following:]
        elif key == "ctrl_k":
            self.buffer = self.buffer[:self.cursor]
        elif key == "ctrl_u":
            self.buffer = self.buffer[self.cursor:]
            self.cursor = 0
        elif key == "ctrl_w":
            self._delete_word(boundaries)
        else:
            return False
        return True

    def _delete_word(self, boundaries: list[int]) -> None:
        position = boundaries.index(self.cursor)
        while position and self.buffer[boundaries[position - 1]:boundaries[position]].isspace():
            position -= 1
        while position and not self.buffer[boundaries[position - 1]:boundaries[position]].isspace():
            position -= 1
        start = boundaries[position]
        self.buffer = self.buffer[:start] + self.buffer[self.cursor:]
        self.cursor = start

    def _recall_history(self, key: str) -> None:
        if key == "up" and self.history_position:
            if self.history_position == len(self.history):
                self._draft = (self.buffer, self.cursor)
            self.history_position -= 1
            self.buffer = self.history[self.history_position]
            self.cursor = len(self.buffer)
        elif key == "down" and self.history_position < len(self.history):
            self.history_position += 1
            if self.history_position == len(self.history):
                self.buffer, self.cursor = self._draft
            else:
                self.buffer = self.history[self.history_position]
                self.cursor = len(self.buffer)

    def _insert_text(self, key: str) -> None:
        text = " " if key == "space" else key
        if len(text) == 1 and (text.isprintable() or text in ("\u200c", "\u200d")):
            self.buffer = self.buffer[:self.cursor] + text + self.buffer[self.cursor:]
            target = self.cursor + 1
            self.cursor = next(pos for pos in _edit_boundaries(self.buffer) if pos >= target)


def _editor_view(editor: _LineEditor, width: int) -> tuple[str, int]:
    """Scroll whole editing units into view, reserving a cell for the cursor."""
    budget = max(0, width - 1)
    boundaries = _edit_boundaries(editor.buffer)
    units = [_safe(editor.buffer[left:right]) for left, right in zip(boundaries, boundaries[1:])]
    widths = [_tui_cell_width(unit) for unit in units]
    start = boundaries.index(editor.cursor)
    cursor_cells = 0
    while start and cursor_cells + widths[start - 1] <= budget:
        start -= 1
        cursor_cells += widths[start]
    end = start
    cells = 0
    while end < len(units) and cells + widths[end] <= budget:
        cells += widths[end]
        end += 1
    return "".join(units[start:end]), cursor_cells


def _install_prompt_signals(saved, interrupt) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        previous = signal.getsignal(signum)
        if previous == signal.SIG_IGN:
            continue
        saved[signum] = previous
        signal.signal(signum, interrupt)


def _forward_prompt_signal(interrupted, saved) -> None:
    signum, frame = interrupted.args
    previous = saved[signum]
    if callable(previous):
        previous(signum, frame)
        return
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)


@contextmanager
def _prompt_signals():
    """Unwind raw mode before catchable termination signals leave a prompt."""
    saved = {}

    # Signal unwinding must bypass ordinary exception handlers before restoring raw mode.
    class Interrupted(BaseException):  # NOSONAR(S5709)
        pass

    def interrupt(signum, frame):
        raise Interrupted(signum, frame)

    interrupted = None
    try:
        try:
            _install_prompt_signals(saved, interrupt)
            yield
        except Interrupted as exc:
            interrupted = exc
    finally:
        for signum, previous in saved.items():
            signal.signal(signum, previous)
        if interrupted is not None:
            _forward_prompt_signal(interrupted, saved)


@contextmanager
def _prompt_raw_mode():
    """Enter prompt raw mode without flushing queued input."""
    if os.name == "nt":
        with _raw_mode():
            yield
        return
    import termios
    with _raw_mode(when=termios.TCSANOW):
        yield


_PROMPT_HISTORY: dict[str, list[str]] = {}
_ARCHIVE_BROWSE_DIRECTORY: Path | None = None


def _draw_editor(editor: _LineEditor, label: str, context: Sequence[str],
                 hint: str | None, size: tuple[int, int]) -> None:
    cols, rows = size
    if _size_mode(*size) == "small":
        _draw_lines(_too_small_lines(), size=size)
        sys.stdout.write(_HIDE_CURSOR)
    else:
        visible, cursor = _editor_view(editor, cols - 3)
        lines = list(context[:max(0, rows - 3)]) + [label, "> " + visible]
        _draw_lines(lines, footer=[hint or "Enter: submit · Esc/Ctrl+C: cancel"], size=size)
        sys.stdout.write(_cursor_at(len(lines), 3 + cursor, size=size) + _SHOW_CURSOR)
    sys.stdout.flush()


def _read_editor_key() -> str | None:
    key = _read_key_timeout(0.1)
    if key is None:
        return None
    # The shared reader pushes back a printable byte after ESC. Only a bare
    # Escape cancels this editor; leave pending text intact.
    if key == "esc" and _UNIX_KEY_PUSHBACK:
        key = "other"
    if _size_mode(*_term_size()) == "small" and key not in ("esc", "ctrl_d"):
        return None
    return key


def _check_editor_action(editor: _LineEditor, action: str | None) -> None:
    if action == "cancel":
        raise _CancelPrompt()
    if action == "eof":
        raise EOFError()
    if action is not None and action != "submit":
        raise _PromptAction(action, editor.buffer)


def _remember_editor_text(text: str, history_key: str) -> None:
    if text:
        history = _PROMPT_HISTORY.setdefault(history_key, [])
        if not history or history[-1] != text:
            history.append(text)


def _edit_line(label: str, initial_text: str, context: Sequence[str], history_key: str,
               *, extra_keys: Mapping[str, str] | None = None, hint: str | None = None) -> str:
    editor = _LineEditor(initial_text, _PROMPT_HISTORY.get(history_key, ()), extra_keys or {})
    drawn_size = None
    redraw = True
    while True:
        with _prompt_signals(), _prompt_raw_mode():
            while True:
                size = _term_size()
                if redraw or size != drawn_size:
                    _draw_editor(editor, label, context, hint, size)
                    drawn_size = size
                    redraw = False
                key = _read_editor_key()
                if key is None:
                    continue
                action = editor.handle(key)
                _check_editor_action(editor, action)
                if action == "submit":
                    _remember_editor_text(editor.buffer, history_key)
                    return editor.buffer
                redraw = True
        # A previously installed signal handler may return normally. Resume
        # editing the same buffer and cursor after it runs in restored mode.
        redraw = True


def _prompt(prompt: str, default: str = "", *, preserve_spaces: bool = False,
            initial_text: str = "", context: Sequence[str] = (),
            history_key: str | None = None, extra_keys: Mapping[str, str] | None = None,
            hint: str | None = None) -> str:
    """Prompt for a line of input, returning *default* on a blank entry."""
    suffix = f" [{_safe(default)}]" if default else ""
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    sys.stdout.write(_SHOW_CURSOR)
    sys.stdout.flush()
    try:
        if tty:
            response = _edit_line(f"  {_safe(prompt)}{suffix}:", initial_text, context,
                                  history_key if history_key is not None else prompt,
                                  extra_keys=extra_keys, hint=hint)
        else:
            response = input(f"  {_safe(prompt)}{suffix}: ")
    except EOFError as exc:
        raise _QuitInteractive() from exc
    except _TerminalEOF as exc:
        raise _QuitInteractive() from exc
    except KeyboardInterrupt as exc:
        if tty:
            raise _CancelPrompt() from exc
        print()
        raise _QuitInteractive() from exc
    finally:
        sys.stdout.write(_HIDE_CURSOR)
        sys.stdout.flush()
    return (response if preserve_spaces else response.strip()) or default


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
        if not _path_usable(path.is_file):
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


def _manual_path_details(raw: str, error: OSError | None) -> list[str]:
    lines: list[str] = []
    leading = len(raw) - len(raw.lstrip(" "))
    trailing = len(raw) - len(raw.rstrip(" "))
    if leading or trailing:
        lines.extend((f"Leading spaces: {leading}", f"Trailing spaces: {trailing}"))
    stripped = raw.strip()
    hint, hint_error = _path_probe(lambda: bool(stripped and stripped != raw
                                   and not Path(raw).exists() and Path(stripped).exists()))
    if hint:
        lines.append("Hint: the stripped path exists.")
    error = error or hint_error
    if error is not None:
        lines.append(_cli_os_reason(error, output=False))
    return lines


def _manual_path_error(raw: str, error: OSError | None = None) -> list[str]:
    """Leave bounded rejection context visible while the next line is read."""
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    cols, rows = _term_size()
    shown = _safe(raw)
    if tty:
        shown = _tui_truncate(shown, max(0, cols - 13))
    lines = ["Not a file.", f'Rejected: "{shown}"']
    lines.extend(_manual_path_details(raw, error))
    lines.extend(("Edit the path below." if tty else "Rejected text is read-only.",
                  "Enter another path; empty returns."))
    if tty:
        lines = lines[:max(0, rows - 2)]
        _draw_lines(lines, size=(cols, rows))
        sys.stdout.write(_cursor_at(min(len(lines) + 1, rows), size=(cols, rows)))
        sys.stdout.flush()
    else:
        for line in lines:
            print("  " + line)
    return lines


def _archive_browse_start(raw: str) -> Path:
    """Use typed path context first; only an empty draft uses session memory."""
    if not raw:
        if _ARCHIVE_BROWSE_DIRECTORY is not None and _path_usable(_ARCHIVE_BROWSE_DIRECTORY.is_dir):
            return _ARCHIVE_BROWSE_DIRECTORY
        return Path.cwd()
    candidate = Path(raw)
    if _path_usable(candidate.is_dir):
        return candidate.absolute()
    if _path_usable(candidate.parent.is_dir):
        return candidate.parent.absolute()
    return Path.cwd()


def _prompt_manual_archive_path(initial_text: str, context: Sequence[str]) -> str:
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _prompt("Archive path", preserve_spaces=True,
                       initial_text=initial_text, context=context,
                       extra_keys={"confirm": "browse"},
                       hint="Enter OK · Tab Browse · Esc/Ctrl+C Back")
    return _prompt("Archive path", preserve_spaces=True)


def _handle_manual_path() -> Path | None:
    _clear_screen()
    print()
    initial_text = ""
    context: list[str] = []
    while True:
        try:
            raw = _prompt_manual_archive_path(initial_text, context)
        except _CancelPrompt:
            return None
        except _PromptAction as action:
            if action.action != "browse":
                raise
            initial_text = action.text
            picked = _select_sources_arrow(_archive_browse_start(initial_text), single=True)
            if picked is None:
                continue
            raw = str(picked[0])
        if not raw:
            return None
        candidate = Path(raw)
        usable, error = _path_probe(candidate.is_file)
        if usable:
            return candidate
        context = _manual_path_error(raw, error)
        initial_text = raw


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


def _menu_password(current: str | None) -> str | None:
    """Use the CLI's secure password inputs without displaying their value."""
    choice = _select_menu("Archive password", [
        ("p", "Prompt securely"),
        ("f", "Read a password file"),
        ("n", "Clear password"),
        ("b", "Back"),
    ])
    if choice == "p":
        return _read_password(argparse.Namespace(password=True, password_file=None))
    if choice == "f":
        _clear_screen()
        try:
            path = _prompt("Password file")
        except _CancelPrompt:
            return current
        if path:
            return _read_password(argparse.Namespace(password=False, password_file=Path(path)))
    if choice == "n":
        return None
    return current


@contextmanager
def _menu_password_errors(password: str | None):
    """Do not expose a password if an optional backend includes it in an error."""
    try:
        yield
    except (ArchiveError, OSError, ValueError) as exc:
        if password and password in str(exc):
            message = str(exc).replace(password, "[redacted]")
            if isinstance(exc, ArchiveError):
                raise type(exc)(message) from None
            raise ArchiveError(message) from None
        raise


def _menu_format_path(path: Path, fmt: ArchiveFormat) -> Path:
    """Keep the output's usual suffix aligned with the selected writer."""
    name = path.name
    for suffix in _ARCHIVE_SUFFIXES:
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
            break
    return path.with_name(name + "." + fmt.value)


def _prompt_compression_level(current: int | None, context: Sequence[str], *, tty: bool) -> str:
    default = "default" if current is None else str(current)
    if tty:
        return _prompt("Compression (0-9/default)", default, context=context,
                       history_key="Compression level (0-9 or default)")
    return _prompt("Compression level (0-9 or default)", default)


def _menu_compression(current: int | None) -> int | None:
    _clear_screen()
    context: list[str] = []
    while True:
        try:
            tty = sys.stdin.isatty() and sys.stdout.isatty()
            raw = _prompt_compression_level(current, context, tty=tty)
        except _CancelPrompt:
            return current
        if raw.lower() == "default":
            return None
        if raw.isascii() and raw.isdecimal() and 0 <= int(raw) <= 9:
            return int(raw)
        message = "Use 0-9, or default." if tty else "Enter a compression level from 0 to 9, or default."
        context = [message]
        if not tty:
            print("  " + message)


def _menu_compression_supported(fmt: ArchiveFormat) -> bool:
    return fmt not in (ArchiveFormat.TAR, ArchiveFormat.CPIO, ArchiveFormat.AR)


def _menu_password_supported(archive: Path) -> bool:
    return detect_format(archive) in (ArchiveFormat.ZIP, ArchiveFormat.ZIPX,
                                      ArchiveFormat.SEVEN_ZIP)


def _menu_unavailable(message: str) -> None:
    _clear_screen()
    with _menu_result_output():
        print(f"  {message}")
    _wait_for_enter()


def _menu_password_label(password: str | None, supported: bool, unavailable: str) -> str:
    if not supported:
        return "Password: unavailable for this " + unavailable
    status = "set" if password is not None else "none"
    return f"Password: {status}"


def _menu_edit_password(current: str | None, supported: bool, message: str) -> str | None:
    if supported:
        return _menu_password(current)
    _menu_unavailable(message)
    return current


@dataclass
class _CreateOptions:
    cwd: Path
    sources: list[Path]
    supported: list[ArchiveFormat]
    fmt: ArchiveFormat
    archive_path: Path
    compresslevel: int | None = None
    password: str | None = None
    symlinks: str = "store"

    def menu_options(self) -> list[tuple[str, str]]:
        shown_sources = ", ".join(_safe(str(path)) for path in self.sources)
        compression = "Compression: unavailable for this writer"
        if _menu_compression_supported(self.fmt):
            level = "default" if self.compresslevel is None else str(self.compresslevel)
            compression = f"Compression: {level}"
        return [
            ("s", f"Sources: {len(self.sources)} selected — {shown_sources}"),
            ("o", f"Output: {_safe(self.archive_path)}"),
            ("f", f"Format: {self.fmt.value}"),
            ("l", compression),
            ("p", _menu_password_label(self.password, self.fmt is ArchiveFormat.SEVEN_ZIP, "writer")),
            ("y", f"Symlinks: {self.symlinks}"),
            ("r", "Create archive"),
            ("b", "Back"),
        ]

    def set_writer(self, fmt: ArchiveFormat) -> None:
        self.fmt = fmt
        if fmt is not ArchiveFormat.SEVEN_ZIP:
            self.password = None
        if not _menu_compression_supported(fmt):
            self.compresslevel = None

    def select_sources(self) -> None:
        picked = _select_sources_arrow(self.cwd)
        if picked:
            self.sources = picked

    def select_output(self) -> None:
        _clear_screen()
        try:
            candidate = self.cwd / _prompt("Output archive", str(self.archive_path))
        except _CancelPrompt:
            return
        if not candidate.name.lower().endswith(_ARCHIVE_SUFFIXES):
            self.archive_path = _menu_format_path(candidate, self.fmt)
            return
        fmt = detect_format(candidate, for_write=True)
        if fmt not in self.supported:
            raise FormatError(f"cannot create {fmt.value}; choose an installed writer")
        self.set_writer(fmt)
        self.archive_path = candidate

    def select_format(self) -> None:
        formats = [(str(index), value.value) for index, value in enumerate(self.supported, 1)]
        formats.append(("b", "Back"))
        selected = _select_menu("Choose an installed writer", formats)
        if selected is not None and selected != "b":
            self.set_writer(self.supported[int(selected) - 1])
            self.archive_path = _menu_format_path(self.archive_path, self.fmt)

    def select_compression(self) -> None:
        if _menu_compression_supported(self.fmt):
            self.compresslevel = _menu_compression(self.compresslevel)
        else:
            _menu_unavailable("This writer does not support a compression level.")

    def select_password(self) -> None:
        self.password = _menu_edit_password(
            self.password, self.fmt is ArchiveFormat.SEVEN_ZIP,
            "Password-protected creation requires an installed 7z writer.",
        )

    def select_symlinks(self) -> None:
        selected = _select_menu("Symlink policy", [
            ("s", "Store links"), ("f", "Follow links"),
            ("k", "Skip links"), ("b", "Back"),
        ])
        self.symlinks = {"s": "store", "f": "follow", "k": "skip"}.get(selected or "", self.symlinks)


def _configure_create(options: _CreateOptions) -> bool:
    handlers = {
        "s": options.select_sources, "o": options.select_output,
        "f": options.select_format, "l": options.select_compression,
        "p": options.select_password, "y": options.select_symlinks,
    }
    while True:
        choice = _select_menu("Create archive options", options.menu_options(), subtitle=str(options.cwd))
        if choice is None or choice == "b":
            return False
        if choice == "r":
            return True
        handlers[choice]()


def _menu_confirm_overwrite(archive: Path) -> bool:
    exists, error = _path_probe(archive.exists)
    if error is not None:
        raise ArchiveError(f"archive {str(archive)!r}: {_cli_os_reason(error, output=True)}") from error
    if not exists:
        return True
    _clear_screen()
    try:
        answer = _prompt(f"{archive} exists. Overwrite? (y/N)", "n",
                         history_key="Overwrite confirmation").lower()
    except _CancelPrompt:
        answer = "n"
    if answer in ("y", "yes"):
        return True
    with _menu_result_output():
        print("  Cancelled; existing archive was not changed.")
    return False


def _print_menu_skipped(names: Sequence[str]) -> None:
    for name in names:
        print(f"  skipped: {_safe(name)}", file=sys.stderr)


def _create_diagnostic_text(value, password: str | None) -> str:
    text = str(value)
    return _safe(text.replace(password, "[redacted]") if password else text)


def _print_create_skips(report: CreateReport, *, password: str | None = None,
                        emit: Callable[[str], None] | None = None) -> None:
    if emit is None:
        emit = lambda message: print(message, file=sys.stderr)
    for skipped in report.skip_errors[:20]:
        path = _create_diagnostic_text(skipped.path, password)
        reason = _create_diagnostic_text(skipped.reason, password)
        emit(f"warning: skipped {path}: {reason}")
    if report.accidental_skips > 20:
        emit(f"warning: {report.accidental_skips - 20} more inputs skipped "
             f"({report.accidental_skips} total)")
    if report.cleanup_warning:
        emit(f"warning: {_create_diagnostic_text(report.cleanup_warning, password)}")


def _print_create_counts(report: CreateReport) -> None:
    print(f"  {report.included_inputs} included, {report.accidental_skips} skipped.")


def _print_menu_create_counts(report: CreateReport) -> None:
    skipped = report.accidental_skips + len(report.skipped)
    print(f"  {report.included_inputs} included, {skipped} skipped.")


def _print_menu_create_error(exc: CreateError, password: str | None) -> None:
    _clear_screen()
    with _menu_result_output():
        print()
        print("  error: existing archive preserved." if exc.preserved_existing
              else "  error: archive was not created.")
        _print_menu_create_counts(exc.report)
        print(f"  {_create_diagnostic_text(exc, password)}")
        _print_create_skips(exc.report, password=password)


def _print_menu_create_result(report: CreateReport, password: str | None) -> None:
    print("  Archive is incomplete." if report.accidental_skips else "  Creation complete.")
    _print_menu_create_counts(report)
    _print_create_summary(report)
    _print_create_skips(report, password=password)
    _print_menu_skipped(report.skipped)


def _print_menu_extract_result(report: ExtractReport) -> None:
    print("  Extraction complete.")
    print(f"  {report.total} included, {len(report.skipped)} skipped.")
    _print_extract_summary(report)
    _print_menu_skipped(report.skipped)


def _print_menu_test_result(report: TestReport) -> None:
    print("Archive is OK.")
    print(f"  {report.entries} entries, {report.files} files, {human_bytes(report.bytes_read)} decoded")
    print(f"Tested {_safe(report.archive)}  [{report.format.value}]")


_UNKNOWN_ARCHIVE_STATE = object()


def _menu_archive_stamp(path: Path | None):
    if path is None:
        return _UNKNOWN_ARCHIVE_STATE
    try:
        value = path.lstat()
        return (value.st_dev, value.st_ino, value.st_ctime_ns, value.st_mtime_ns, value.st_size)
    except FileNotFoundError:
        return None
    except OSError:
        return _UNKNOWN_ARCHIVE_STATE


def _menu_create_cancel_message(archive: Path | None, before) -> str:
    after = _menu_archive_stamp(archive)
    if before is _UNKNOWN_ARCHIVE_STATE or after is _UNKNOWN_ARCHIVE_STATE:
        return "Archive state could not be verified; check the output path."
    if before is not _UNKNOWN_ARCHIVE_STATE and after == before:
        return "No archive was created; any existing archive was left unchanged."
    if after is not None and after is not _UNKNOWN_ARCHIVE_STATE:
        return "The current archive was kept; it may already have been published."
    return "Archive state could not be verified; check the output path."


def _print_menu_cancel(operation: str, archive: Path | None = None,
                       before=_UNKNOWN_ARCHIVE_STATE, notes: Sequence[str] = ()) -> None:
    _clear_screen()
    with _menu_result_output():
        if operation == "create":
            print("Create cancelled.")
            print(_menu_create_cancel_message(archive, before))
        elif operation == "extract":
            print("Extraction cancelled.")
            print("Files already published were kept; unfinished staged files were removed where possible.")
        else:
            print("Test cancelled. No changes were made.")
        if archive is not None:
            print(f"{'Output' if operation == 'create' else 'Archive'}: {_safe(archive)}")
        for note in notes:
            print(f"warning: {note}")


@contextmanager
def _menu_operation_signals():
    """Let the first SIGINT cancel; protect engine unwinding from a second."""
    previous = signal.getsignal(signal.SIGINT)

    def interrupt(signum, frame):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


_MenuReport = TypeVar("_MenuReport", CreateReport, ExtractReport, TestReport)


def _menu_run_operation(operation: str, action: Callable[[_TuiProgressDisplay], _MenuReport], *,
                        password: str | None = None, archive: Path | None = None) -> _MenuReport | None:
    display = _TuiProgressDisplay(operation)
    before = _menu_archive_stamp(archive) if operation == "create" else _UNKNOWN_ARCHIVE_STATE
    with _menu_operation_signals():
        try:
            with _menu_password_errors(password):
                report = action(display)
            skipped = len(report.skipped) if isinstance(report, (CreateReport, ExtractReport)) else 0
            if isinstance(report, CreateReport):
                skipped += report.accidental_skips
            display.finish(skipped)
            return report
        except _TerminalEOF:
            raise
        except KeyboardInterrupt as exc:
            display.abort()
            notes = [_create_diagnostic_text(note, password) for note in getattr(exc, "__notes__", ())]
            _print_menu_cancel(operation, archive, before, notes)
            return None
        finally:
            display.abort()


def _menu_create_archive(engine: ArchiveEngine, options: _CreateOptions, precollected) -> CreateReport | None:
    try:
        return _menu_run_operation("create", lambda display: engine.create(
            options.archive_path, options.sources, fmt=options.fmt, root=options.cwd,
            compresslevel=options.compresslevel, symlinks=options.symlinks,
            password=options.password, progress=display, precollected=precollected),
            password=options.password, archive=options.archive_path)
    except CreateError as exc:
        _print_menu_create_error(exc, options.password)
        return None


def _menu_create(engine: ArchiveEngine) -> None:
    cwd = Path.cwd()
    sources = _select_sources_arrow(cwd)
    if not sources:
        _clear_screen()
        with _menu_result_output():
            print("\n  No sources selected; cancelled.")
        return
    supported = engine.writable_formats()
    if not supported:
        raise FormatError("no archive writers are available")
    fmt = ArchiveFormat.TAR_GZ if ArchiveFormat.TAR_GZ in supported else supported[0]
    options = _CreateOptions(cwd, sources, supported, fmt, cwd / ("archive." + fmt.value))
    if not _configure_create(options) or not _menu_confirm_overwrite(options.archive_path):
        return

    _clear_screen()
    print()
    archive_real = os.path.realpath(options.archive_path)
    skip_errors: list[CreateSkip] = []
    try:
        precollected = _interactive_enumerate(
            options.sources,
            root=cwd,
            symlinks=options.symlinks,
            archive_real=archive_real,
            skip_errors=skip_errors,
        )
    except KeyboardInterrupt:
        _clear_screen()
        with _menu_result_output():
            print(_CANCELLED_MESSAGE)
        return
    except Exception as exc:
        if not skip_errors:
            raise
        report = CreateReport(options.archive_path, options.fmt,
                              accidental_skips=len(skip_errors), skip_errors=skip_errors)
        _print_menu_create_error(
            CreateError(str(exc), report, preserved_existing=os.path.lexists(options.archive_path)),
            options.password,
        )
        return
    if precollected is None:
        _clear_screen()
        with _menu_result_output():
            print("\n  Enumeration cancelled.")
        return

    print()
    report = _menu_create_archive(engine, options, precollected)
    if report is None:
        return
    with _menu_result_output():
        _print_menu_create_result(report, options.password)


def _menu_selected_members(names: list[str], selected: set[str]) -> list[str] | None:
    if selected == set(names):
        return None
    return [name for name in names if name in selected]


class _MembersScreen(_MenuScreen):
    """Edit exact member names on one persistent menu, with rollback on exit."""

    def __init__(self, names: list[str], current: list[str] | None) -> None:
        self.names = names
        self.marked = set(names if current is None else current)
        self._original_marks = set(self.marked)
        self._original = current
        self.members_result = current
        self._notice: str | None = None
        super().__init__("Choose exact archive members", None, [])
        self._refresh_options()

    def _refresh_options(self) -> None:
        count = sum(name in self.marked for name in self.names)
        self.title = f"Marked ({count}) · Exact archive members"
        self.options = [
            ("a", "Select all members"), ("n", "Select no members"),
            ("r", "Use this selection"), ("b", "Back"),
        ]
        self.options.extend((str(index), f"[{'x' if name in self.marked else ' '}] {_safe(name)}")
                            for index, name in enumerate(self.names, 1))

    def draw(self, cols: int, rows: int) -> list[str]:
        lines = super().draw(cols, rows)
        if not self._notice or self._mode == "small":
            return lines
        notice = "  " + _paint(self._notice, _C.BOLD_YELLOW)
        if self._mode == "compact":
            room = rows - 2 - len(self.footer())
            return [lines[0], notice] + [self._option_line(i) for i in self._compact_indices(room)]
        title_row = "  " + _paint(self.title, _C.BOLD)
        lines[lines.index(title_row) + 1] = notice
        return lines

    def footer(self) -> list[str]:
        if self._mode == "small":
            return []
        if self._mode == "compact":
            return [_paint("Space Mark Tab/r OK Esc/Ctrl+C Cancel", _C.DIM)]
        return [
            "  " + _paint("Space toggles member · Enter activates row · Tab / r confirm", _C.DIM),
            "  " + _paint("↑↓ move · a all · n none · Esc/b/q/Q/Ctrl+C discard", _C.DIM),
        ]

    def _activate(self, choice: str | None) -> _ExitMarker | None:
        if choice is None or choice == "b":
            self.marked = set(self._original_marks)
            self.members_result = self._original
            self._notice = None
            self._refresh_options()
            return _EXIT
        if choice == "r":
            if not any(name in self.marked for name in self.names):
                self._notice = "Nothing marked. Space marks; Tab OK."
                return None
            self.members_result = _menu_selected_members(self.names, self.marked)
            return _EXIT
        if choice == "a":
            self.marked = set(self.names)
        elif choice == "n":
            self.marked.clear()
        else:
            self.marked.symmetric_difference_update((self.names[int(choice) - 1],))
        self._notice = None
        self._refresh_options()
        return None

    def handle(self, key: str) -> "Screen | _ExitMarker | None":
        if key in ("up", "down"):
            return super().handle(key)
        if key in ("esc", "quit", "q", "Q"):
            return self._activate(None)
        if key == "confirm":
            return self._activate("r")
        if key == "enter":
            return self._activate(self.options[self.selected][0])
        if key == "space":
            choice = self.options[self.selected][0]
            return self._activate(choice) if choice.isdecimal() else None
        if _match_option_key(key, self.options):
            return self._activate(key)
        return None


def _menu_members(engine: ArchiveEngine, archive: Path, password: str | None,
                  current: list[str] | None) -> list[str] | None:
    with _menu_password_errors(password):
        entries = engine.inspect(archive, password=password).entries
    names = list(dict.fromkeys(entry.name for entry in entries))
    screen = _MembersScreen(names, current)
    if sys.stdin.isatty() and sys.stdout.isatty():
        _run_screen(screen)
    else:
        while True:
            choice = _select_menu(screen.title, screen.options)
            if screen._activate(choice) is _EXIT:
                break
            if screen._notice:
                print(f"  {screen._notice}")
    return screen.members_result


_LimitNumber = TypeVar("_LimitNumber", int, float)


def _menu_limit_label(value: int | float | None) -> str:
    return "none" if value is None else str(value)


def _menu_limit_value(current: _LimitNumber | None,
                      parser: Callable[[str], _LimitNumber]) -> _LimitNumber | None:
    _clear_screen()
    while True:
        raw = _prompt("Limit (or none)", _menu_limit_label(current))
        try:
            if raw.lower() == "none":
                return None
            return parser(raw)
        except (ValueError, argparse.ArgumentTypeError) as exc:
            print(f"  {_safe(exc)}")


class _LimitsScreen(_MenuScreen):
    """Keep limit edits local until Apply; Escape discards a field or the screen."""

    _FIELDS = (
        ("e", "Selected entries", "max_entries"),
        ("s", "Total decoded bytes", "max_total_size"),
        ("f", "Decoded bytes per member", "max_file_size"),
        ("r", "Compression ratio", "max_ratio"),
    )

    def __init__(self, current: ExtractionLimits) -> None:
        self._original = current
        self.values = {field: getattr(current, field) for _, _, field in self._FIELDS}
        self.limits_result = current
        self.editing: str | None = None
        self.buffer = ""
        self._notice: str | None = None
        super().__init__("Extraction limits", "K/KB decimal · KiB binary · none clears", [])
        self._refresh_options()

    def _refresh_options(self) -> None:
        self.options = [(key, f"{label}: {_menu_limit_label(self.values[field])}")
                        for key, label, field in self._FIELDS]
        self.options.extend((("a", "Apply limits"), ("b", "Back (discard edits)")))

    def draw(self, cols: int, rows: int) -> list[str]:
        if self.editing is None:
            return super().draw(cols, rows)
        self._mode = _size_mode(cols, rows)
        if self._mode == "small":
            return _too_small_lines()
        _, label, field = next(item for item in self._FIELDS if item[0] == self.editing)
        current = _menu_limit_label(self.values[field])
        lines = [
            _paint(label, _C.BOLD),
            f"Current: {current}",
            f"Value: {_safe(self.buffer)}" if self.buffer else f"Value: [{current}]",
        ]
        if self._notice:
            lines.append(_paint("error: " + self._notice, _C.BOLD_YELLOW))
        lines.append("Empty keeps value · none clears")
        if self.editing in ("s", "f"):
            lines.append("K/KB decimal · KiB binary")
        return lines[:rows - len(self.footer())]

    def footer(self) -> list[str]:
        if self._mode == "small":
            return []
        if self.editing is not None:
            hint = "Enter Save · Esc/Ctrl+C Discard field"
            return [_paint(hint, _C.DIM)] if self._mode == "compact" else [
                "  " + _paint(hint, _C.DIM),
                "  " + _paint("Apply on the limits screen keeps edits", _C.DIM),
            ]
        if self._mode == "compact":
            return [_paint("Tab/a Apply b/Esc/q/Q/Ctrl+C Discard", _C.DIM)]
        return [
            "  " + _paint("↑↓ move · Enter activates · e/s/f/r edit", _C.DIM),
            "  " + _paint("Tab/a apply · b/Esc/q/Q/Ctrl+C discard edits", _C.DIM),
        ]

    def _handle_field(self, key: str) -> None:
        if key == "esc":
            self.editing = None
            self.buffer = ""
            self._notice = None
        elif key == "enter" and self.editing is not None:
            field = next(field for shortcut, _, field in self._FIELDS if shortcut == self.editing)
            raw = self.buffer.strip() or _menu_limit_label(self.values[field])
            parser = {"e": _parse_count, "s": parse_size, "f": parse_size, "r": _parse_ratio}[self.editing]
            try:
                value = None if raw.lower() == "none" else parser(raw)
                candidate = dict(self.values)
                candidate[field] = value
                ExtractionLimits(**candidate)
            except (ValueError, argparse.ArgumentTypeError) as exc:
                self._notice = _safe(exc)
                return
            self.values = candidate
            self.editing = None
            self.buffer = ""
            self._notice = None
            self._refresh_options()
        elif key == "backspace":
            self.buffer = self.buffer[:-1]
        elif key == "space":
            self.buffer += " "
        elif len(key) == 1 and key.isprintable():
            self.buffer += key

    def handle(self, key: str) -> "Screen | _ExitMarker | None":
        if self.editing is not None:
            self._handle_field(key)
            return None
        choice = self.options[self.selected][0] if key == "enter" else key
        if choice in ("b", "esc", "quit", "q", "Q"):
            self.values = {field: getattr(self._original, field) for _, _, field in self._FIELDS}
            self.limits_result = self._original
            self._refresh_options()
            return _EXIT
        if key in ("up", "down"):
            return super().handle(key)
        if choice in ("a", "confirm"):
            self.limits_result = ExtractionLimits(**self.values)
            return _EXIT
        if any(shortcut == choice for shortcut, _, _ in self._FIELDS):
            self.editing = choice
            self.buffer = ""
            self._notice = None
        return None


def _menu_limits_plain(current: ExtractionLimits) -> ExtractionLimits:
    entries, total, per_file, ratio = (current.max_entries, current.max_total_size,
                                      current.max_file_size, current.max_ratio)
    while True:
        choice = _select_menu("Extraction limits", [
            ("e", f"Selected entries: {_menu_limit_label(entries)}"),
            ("s", f"Total decoded bytes: {_menu_limit_label(total)}"),
            ("f", f"Decoded bytes per member: {_menu_limit_label(per_file)}"),
            ("r", f"Compression ratio: {_menu_limit_label(ratio)}"),
            ("b", "Use these limits"),
        ], subtitle="No limits by default. K/KB are decimal; KiB is binary. Enter none to clear.")
        if choice is None:
            return current
        if choice == "b":
            return ExtractionLimits(entries, total, per_file, ratio)
        if choice == "e":
            entries = _menu_limit_value(entries, _parse_count)
        elif choice == "s":
            total = _menu_limit_value(total, parse_size)
        elif choice == "f":
            per_file = _menu_limit_value(per_file, parse_size)
        elif choice == "r":
            ratio = _menu_limit_value(ratio, _parse_ratio)


def _menu_limits(current: ExtractionLimits) -> ExtractionLimits:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _menu_limits_plain(current)
    screen = _LimitsScreen(current)
    try:
        _run_screen(screen)
    except _TerminalEOF as exc:
        if screen.editing is not None:
            raise _QuitInteractive() from exc
        raise
    return screen.limits_result


@dataclass
class _ExtractOptions:
    archive: Path
    password_supported: bool
    destination: str = "."
    members: list[str] | None = None
    overwrite: bool = True
    metadata: bool = True
    symlinks: str = "store"
    strip_components: int = 0
    password: str | None = None
    limits: ExtractionLimits = field(default_factory=ExtractionLimits)

    def menu_options(self) -> list[tuple[str, str]]:
        limited = any(value is not None for value in (
            self.limits.max_entries, self.limits.max_total_size,
            self.limits.max_file_size, self.limits.max_ratio,
        ))
        return [
            ("d", f"Destination: {_safe(self.destination)}"),
            ("m", "Members: all" if self.members is None else f"Members: {len(self.members)} exact member(s)"),
            ("o", f"Overwrite: {'yes' if self.overwrite else 'no'}"),
            ("t", f"Metadata: {'preserve' if self.metadata else 'skip'}"),
            ("y", f"Symlinks: {self.symlinks}"),
            ("s", f"Strip components: {self.strip_components}"),
            ("p", _menu_password_label(self.password, self.password_supported, "format")),
            ("l", f"Limits: {'set' if limited else 'none'}"),
            ("r", "Extract archive"), ("b", "Back"),
        ]

    def select_destination(self) -> None:
        _clear_screen()
        try:
            self.destination = _prompt("Destination directory", self.destination)
        except _CancelPrompt:
            return

    def select_members(self, engine: ArchiveEngine) -> None:
        self.members = _menu_members(engine, self.archive, self.password, self.members)

    def toggle_overwrite(self) -> None:
        self.overwrite = not self.overwrite

    def toggle_metadata(self) -> None:
        self.metadata = not self.metadata

    def toggle_symlinks(self) -> None:
        self.symlinks = "skip" if self.symlinks == "store" else "store"

    def select_strip_components(self) -> None:
        _clear_screen()
        context: list[str] = []
        while True:
            tty = sys.stdin.isatty() and sys.stdout.isatty()
            try:
                if tty:
                    raw = _prompt("Strip components", str(self.strip_components), context=context)
                else:
                    raw = _prompt("Strip components", str(self.strip_components))
                self.strip_components = _parse_count(raw)
                return
            except _CancelPrompt:
                return
            except argparse.ArgumentTypeError as exc:
                context = [_safe(exc)]
                if not tty:
                    print(f"  {_safe(exc)}")

    def select_password(self) -> None:
        self.password = _menu_edit_password(
            self.password, self.password_supported,
            "This format does not support passwords in Snug.",
        )

    def select_limits(self) -> None:
        self.limits = _menu_limits(self.limits)


def _configure_extract(engine: ArchiveEngine, options: _ExtractOptions) -> bool:
    handlers = {
        "d": options.select_destination, "m": lambda: options.select_members(engine),
        "o": options.toggle_overwrite, "t": options.toggle_metadata,
        "y": options.toggle_symlinks, "s": options.select_strip_components,
        "p": options.select_password, "l": options.select_limits,
    }
    while True:
        choice = _select_menu("Extract archive options", options.menu_options(), subtitle=str(options.archive))
        if choice is None or choice == "b":
            return False
        if choice == "r":
            if options.members == []:
                with _menu_result_output():
                    print("  No members selected; cancelled.")
                return False
            return True
        handlers[choice]()


def _menu_extract(engine: ArchiveEngine) -> None:
    archive = _select_archive()
    if archive is None:
        _clear_screen()
        with _menu_result_output():
            print(_CANCELLED_MESSAGE)
        return

    options = _ExtractOptions(archive, _menu_password_supported(archive))
    if not _configure_extract(engine, options):
        return

    _clear_screen()
    print(f"\n  Extracting {_safe(archive.name)} into {_safe(options.destination)}…\n")
    report = _menu_run_operation("extract", lambda display: engine.extract(
        archive, options.destination, members=options.members, overwrite=options.overwrite,
        preserve_metadata=options.metadata, symlinks=options.symlinks,
        strip_components=options.strip_components, password=options.password,
        limits=options.limits, progress=display), password=options.password, archive=archive)
    if report is None:
        return
    with _menu_result_output():
        _print_menu_extract_result(report)


def _menu_test(engine: ArchiveEngine) -> None:
    archive = _select_archive()
    if archive is None:
        return
    password: str | None = None
    password_supported = _menu_password_supported(archive)
    while True:
        choice = _select_menu("Test archive integrity", [
            ("p", _menu_password_label(password, password_supported, "format")),
            ("r", "Test archive"), ("b", "Back"),
        ], subtitle=str(archive))
        if choice is None or choice == "b":
            return
        if choice == "p":
            password = _menu_edit_password(password, password_supported,
                                           "This format does not support passwords in Snug.")
        elif choice == "r":
            break
    _clear_screen()
    report = _menu_run_operation("test", lambda display: engine.test(
        archive, password=password, progress=display), password=password, archive=archive)
    if report is None:
        return
    with _menu_result_output():
        _print_menu_test_result(report)


def _menu_list(engine: ArchiveEngine) -> None:
    archive = _select_archive()
    if archive is None:
        _clear_screen()
        with _menu_result_output():
            print(_CANCELLED_MESSAGE)
        return

    _clear_screen()
    entries = engine.list_entries(archive)
    with _menu_result_output():
        print(f"\n  {_safe(archive.name)} — {len(entries)} entries\n")
        for entry in entries:
            _print_entry_verbose(entry)
        print()


def _menu_info(engine: ArchiveEngine) -> None:
    archive = _select_archive()
    if archive is None:
        _clear_screen()
        with _menu_result_output():
            print(_CANCELLED_MESSAGE)
        return

    _clear_screen()
    data = engine.info(archive)
    width = max(len(k) for k in data)
    with _menu_result_output():
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
    ("5", "Test archive integrity"),
    ("0", "Quit"),
]

_MENU_HANDLERS: dict[str, Callable[[ArchiveEngine], None]] = {
    "1": _menu_create,
    "2": _menu_extract,
    "3": _menu_list,
    "4": _menu_info,
    "5": _menu_test,
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
        with _menu_result_output():
            print(f"\n  error: unsafe archive: {_safe(exc)}\n")
    except ArchiveError as exc:
        _clear_screen()
        with _menu_result_output():
            print(f"\n  error: {_safe(exc)}\n")
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError,
            ValueError) as exc:
        _clear_screen()
        with _menu_result_output():
            print(f"\n  error: {_safe(exc)}\n")

    _wait_for_enter()

    return None


def _run_menu_choice(engine: ArchiveEngine) -> int | None:
    """Run one pass of the top-level menu."""
    try:
        choice = _select_menu(
            "Select an action",
            _MENU_OPTIONS,
            subtitle=f"  Working directory: {Path.cwd()}",
            cancel_on_interrupt=False,
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
    global _ARCHIVE_BROWSE_DIRECTORY
    _ARCHIVE_BROWSE_DIRECTORY = None
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

def _parse_count(text: str) -> int:
    if not text.isascii() or not text.isdigit():
        raise argparse.ArgumentTypeError('count must be a nonnegative integer')
    return int(text)


def _parse_size_argument(text: str) -> int:
    try:
        return parse_size(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_ratio(text: str) -> float:
    import math
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('ratio must be a finite positive number') from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('ratio must be a finite positive number')
    return value


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

    x = sub.add_parser("extract", help="extract an archive", allow_abbrev=False)
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
    x.add_argument('--max-files', type=_parse_count, default=None, metavar='N', help='maximum selected entry count')
    x.add_argument('--max-size', type=_parse_size_argument, default=None, metavar='SIZE', help='maximum decoded bytes (K/KB decimal, KiB binary)')
    x.add_argument('--max-file-size', type=_parse_size_argument, default=None, metavar='SIZE', help='maximum decoded bytes per member')
    x.add_argument('--max-ratio', type=_parse_ratio, default=None, metavar='RATIO', help='maximum ratio when reliable member sizes exist')

    l = sub.add_parser("list", help="list archive contents")
    l.add_argument("archive")
    l.add_argument("-v", "--verbose", action="store_true",
                   help="show size, mode and modification time")

    i = sub.add_parser("info", help="show archive summary")
    i.add_argument("archive")

    t = sub.add_parser("test", help="verify archive payload integrity", allow_abbrev=False)
    t.add_argument("archive")
    t.add_argument("-q", "--quiet", action="store_true")

    d = sub.add_parser("doctor", help="report offline installation health", allow_abbrev=False)
    d.add_argument("--json", action="store_true", help="print a stable JSON report")
    f = sub.add_parser("formats", help="show installed archive capabilities", allow_abbrev=False)
    f.add_argument("--json", action="store_true", help="print a stable JSON report")

    u = sub.add_parser("update", help="check for releases or update a managed installation")
    update_action = u.add_mutually_exclusive_group()
    update_action.add_argument("--check", action="store_true", help="check for a newer stable release")
    update_action.add_argument("--enable-checks", action="store_true", help="enable daily automatic checks")
    update_action.add_argument("--disable-checks", action="store_true", help="disable automatic checks")

    for command in (c, x, l, i, t):
        passwords = command.add_mutually_exclusive_group()
        passwords.add_argument("--password", action="store_true",
                               help="securely prompt for a password")
        passwords.add_argument("--password-file", type=Path, metavar="FILE",
                               help="read a UTF-8 password from a file")

    return parser


def _cli_progress_width(stream) -> int:
    try:
        return max(os.get_terminal_size(stream.fileno()).columns, 1)
    except (AttributeError, OSError, ValueError):
        return 80


def _cli_progress_number(value: int | float, *, byte_units: bool = False) -> str:
    scale = 1024 if byte_units else 1000
    number = max(value, 0)
    for unit in ("", "K", "M", "G", "T", "P", "E"):
        if unit == "E" and number >= scale:
            return f"{value:.1g}".replace("e+", "e") + ("B" if byte_units else "")
        if number < scale or unit == "E":
            suffix = "B" if byte_units and not unit else unit
            return f"{number:.3g}{suffix}".replace("e+", "e")
        number /= scale
    return "0"


def _cli_progress_counter(display: "_CliProgressDisplay", compact: bool) -> str:
    if display.total_bytes and not display._skipped:
        if compact:
            return (f"{_cli_progress_number(display.bytes_done, byte_units=True)}/"
                    f"{_cli_progress_number(display.total_bytes, byte_units=True)}")
        return f"{human_bytes(display.bytes_done)}/{human_bytes(display.total_bytes)}"
    processed = display.total_items if display._complete else display.items_done
    if compact:
        return f"{_cli_progress_number(processed)}/{_cli_progress_number(display.total_items)}"
    return f"{processed}/{display.total_items} items"


def _cli_progress_state(display: "_CliProgressDisplay", width: int) -> str:
    if not display.total_bytes:
        return "done" if display._complete else "working"
    precision = 0 if width < 59 else 1
    return f"{display._fraction() * 100:.{precision}f}%"


def _cli_progress_fit(display: "_CliProgressDisplay", width: int, counter: str, compact: str) -> str:
    state = _cli_progress_state(display, width)
    skipped = f"skip {_cli_progress_number(display._skipped)}" if display._skipped else ""
    candidates = ((display.operation, counter), (display.operation, compact),
                  (display.operation[:2], compact), ("", compact))
    for operation, units in candidates:
        line = " ".join(part for part in (operation, state, units, skipped) if part)
        if _cell_width(line) <= width:
            return line
    processed = _cli_progress_number(display.total_items)
    return f"{state} {processed} {skipped}".rstrip()


def _cli_progress_essential(display: "_CliProgressDisplay", width: int) -> str:
    if not display.total_bytes and (display.bytes_done or
                                   (display.total_items <= 1 and display.operation != "create")):
        counter = _cli_progress_number(display.bytes_done, byte_units=True)
        return _cli_progress_fit(display, width, counter, counter)
    return _cli_progress_fit(display, width,
                             _cli_progress_counter(display, compact=width < 59),
                             _cli_progress_counter(display, compact=True))


def _cli_progress_fields(display: "_CliProgressDisplay", width: int, elapsed: float) -> list[str]:
    speed = display.bytes_done / elapsed
    fields = []
    if width >= 39 and display.total_bytes:
        filled = int(display._fraction() * 12)
        fields.append("[" + "=" * filled + "-" * (12 - filled) + "]")
    if width >= 59:
        remaining = max(display.total_bytes - display.bytes_done, 0)
        detail = f"ETA {human_time(remaining / speed if speed else float('inf'))}"
        fields.append(detail if display.total_bytes and not display._complete else f"elapsed {human_time(elapsed)}")
    if width >= 79 and (display.total_bytes or display.bytes_done or display.operation != "create"):
        fields.append(f"{_cli_progress_number(speed, byte_units=True)}/s")
    return fields


def _cli_progress_line(display: "_CliProgressDisplay", width: int, elapsed: float) -> str:
    line = _cli_progress_essential(display, width)
    for field in _cli_progress_fields(display, width, elapsed):
        candidate = f"{line} | {field}"
        if _cell_width(candidate) > width:
            break
        line = candidate
    available = width - _cell_width(line) - 3
    if display.current and available > 0:
        line += " | " + _truncate_cells(display.current, available)
    return _truncate_cells(line, width)


class _CliProgressDisplay(ProgressDisplay):
    """Render bounded CLI progress without changing the TUI renderer."""

    def __init__(self, operation: str, quiet: bool = False, stream=None,
                 *, defer_final: bool = False) -> None:
        super().__init__(operation, quiet=quiet, stream=stream)
        self._line_open = False
        self._defer_final = defer_final
        self._complete = False
        self._skipped = 0

    def done(self) -> None:
        if not self._defer_final:
            self.finish()

    def finish(self, skipped_count: int = 0) -> None:
        if self._complete:
            return
        self._complete = True
        self._skipped = skipped_count
        self._render(force=True, final=True)

    def _fraction(self) -> float:
        return 1.0 if self._complete else super()._fraction()

    def _render_plain(self, final: bool, elapsed: float, speed: float) -> None:
        if not self._skipped:
            super()._render_plain(final, elapsed, speed)
            return
        if not final:
            return
        detail = (f", {human_bytes(self.bytes_done)} in {human_time(elapsed)} "
                  f"(avg {human_bytes(speed)}/s)" if self.total_bytes else
                  f" in {human_time(elapsed)}")
        self.stream.write(f"{self.operation}: {self.total_items}/{self.total_items} items"
                          f"{detail}, {self._skipped} skipped\n")
        self.stream.flush()

    def _closed_pipe(self) -> None:
        self.quiet = True
        self._line_open = False
        _silence_closed_pipe(self.stream)

    def _render(self, force: bool = False, final: bool = False) -> None:
        if self.quiet:
            return
        now = time.monotonic()
        if not force and now - self._last_render < _TTY_REFRESH:
            return
        self._last_render = now
        elapsed = max(now - self.start_time, 1e-9)
        try:
            if self.tty:
                self._line_open = not final
                line = _cli_progress_line(self, _cli_progress_width(self.stream) - 1, elapsed)
                self.stream.write("\r" + line + "\x1b[K" + ("\n" if final else ""))
                self.stream.flush()
            else:
                self._render_plain(final, elapsed, self.bytes_done / elapsed)
        except BrokenPipeError:
            self._closed_pipe()

    def abort(self) -> None:
        if not self._line_open:
            return
        self._line_open = False
        try:
            self.stream.write("\n")
            self.stream.flush()
        except BrokenPipeError:
            self._closed_pipe()


class _TuiProgressDisplay(_CliProgressDisplay):
    """Use the CLI's counters and completion rules within the TUI cell budget."""

    def __init__(self, operation: str, stream=None) -> None:
        super().__init__(operation, stream=sys.stdout if stream is None else stream, defer_final=True)

    def _render(self, force: bool = False, final: bool = False) -> None:
        if self.quiet:
            return
        now = time.monotonic()
        if not force and now - self._last_render < _TTY_REFRESH:
            return
        self._last_render = now
        elapsed = max(now - self.start_time, 1e-9)
        if self.tty:
            line = _cli_progress_line(self, max(1, _term_size()[0] - 1), elapsed)
            self._line_open = not final
            self.stream.write("\r" + line + _CLEAR_LINE + ("\n" if final else ""))
            self.stream.flush()
        else:
            self._render_plain(final, elapsed, self.bytes_done / elapsed)


def _silence_closed_pipe(stream) -> None:
    """Keep later buffered flushes away from a closed consumer."""
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        replacement = open(os.devnull, "w")
        if stream is sys.stdout:
            sys.stdout = replacement
        elif stream is sys.stderr:
            sys.stderr = replacement
        else:
            replacement.close()
        return
    null_descriptor = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_descriptor, descriptor)
    finally:
        os.close(null_descriptor)


def _run_with_closed_stdout(action: Callable[[], int]) -> int:
    """Treat an early-closing report consumer as a successful command."""
    try:
        status = action()
        sys.stdout.flush()
        return status
    except BrokenPipeError:
        _silence_closed_pipe(sys.stdout)
        return 0


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


def _cli_exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        chain.append(exc)
        if exc.__cause__ is None:
            break
        exc = exc.__cause__
    return chain


def _cli_error_context(args, member: str = "", *, output: bool = False) -> str:
    context = f"archive {str(args.archive)!r}"
    if output and args.command == "extract":
        context += f", destination {str(args.directory)!r}"
    if member:
        context += f", member {member!r}"
    return context


def _cli_same_path(left, right) -> bool:
    if left is None or right is None:
        return False
    return os.path.abspath(os.fspath(left)) == os.path.abspath(os.fspath(right))


def _cli_create_input_path(args, filename) -> bool:
    if filename is None or _cli_same_path(filename, Path(args.archive).parent):
        return False
    source_path = os.path.abspath(os.fspath(filename))
    for source in getattr(args, "sources", ()):
        root = os.path.abspath(os.fspath(source))
        try:
            if os.path.commonpath((source_path, root)) == root:
                return True
        except ValueError:
            continue
    return False


def _cli_os_context(args, exc: OSError, member: str) -> tuple[str, bool]:
    password_file = getattr(args, "password_file", None)
    filename = exc.filename
    input_file = _cli_same_path(filename, args.archive)
    password_error = _cli_same_path(filename, password_file)
    source_error = args.command == "create" and _cli_create_input_path(args, filename)
    output = args.command in {"create", "extract"} and not (input_file or password_error or source_error)
    context = _cli_error_context(args, member, output=output)
    if password_error:
        context += f", password file {str(password_file)!r}"
    elif source_error:
        context += f", file {str(filename)!r}"
    return context, output


def _cli_os_reason(exc: OSError, *, output: bool) -> str:
    import errno

    if exc.errno in (errno.EACCES, errno.EPERM):
        advice = "choose a writable destination" if output else "check the file permissions"
        return f"permission denied; {advice}"
    messages = {
        errno.ENOSPC: "no space left on the device; free space or choose another destination",
        errno.ENAMETOOLONG: "file name or path is too long; use a shorter name or destination path",
        errno.ENOENT: "file or directory does not exist; check the path",
        errno.ENOTDIR: "a path component is not a directory; choose a directory path",
        errno.EISDIR: "the path is a directory; choose a file path",
        errno.EROFS: "destination is read-only; choose a writable destination",
    }
    fallback = exc.strerror or "file operation failed; check the input and destination paths"
    return fallback if exc.errno is None else messages.get(exc.errno, fallback)


def _cli_os_error(args, exc: OSError, member: str) -> str:
    context, output = _cli_os_context(args, exc, member)
    return f"{context}: {_cli_os_reason(exc, output=output)}"


def _cli_limit_advice(message: str) -> str:
    if message.startswith("archive contains "):
        return "extract fewer members or adjust --max-files"
    limit = message.rsplit(" exceeds ", 1)[-1]
    if limit.startswith("file limit "):
        return "extract fewer members or adjust --max-file-size"
    if limit.startswith("compression ratio limit "):
        return "extract fewer members or adjust --max-ratio"
    return "extract fewer members or adjust --max-size"


def _cli_password_error(chain: Sequence[BaseException]) -> str | None:
    for error in chain:
        message = str(error)
        if message.endswith("could not read 7z archive: incorrect password or corrupt archive"):
            return "incorrect password or corrupt archive; check the password or obtain a complete copy"
        if (message == "7z archive requires a password; use --password or --password-file"
                or error.__class__.__name__ == "PasswordRequired"):
            return "a password is required; use --password or --password-file"
        if isinstance(error, RuntimeError):
            if message.startswith("Bad password for file "):
                return "incorrect password; check the password and try again"
            if message.endswith("is encrypted, password required for extraction"):
                return "a password is required; use --password or --password-file"
    return None


def _cli_truncated_error(chain: Sequence[BaseException]) -> bool:
    for error in chain:
        if isinstance(error, EOFError):
            return True
        message = str(error)
        if isinstance(error, tarfile.ReadError) and message == "unexpected end of data":
            return True
        if message.startswith(("truncated archive member:", "truncated 7z member:",
                               "truncated RAR5 archive header")):
            return True
        if message.startswith("TAR archive ") and message.endswith(" is truncated"):
            return True
    return False


def _cli_corruption_error(chain: Sequence[BaseException]) -> str | None:
    zip_errors = [error for error in chain if isinstance(error, zipfile.BadZipFile)]
    if any(str(error).startswith("Bad CRC-32 for file ") for error in zip_errors) or any(
            error.__class__.__name__ == "CrcError" for error in chain):
        return "member data failed its integrity check; obtain a complete copy of the archive"
    if zip_errors:
        return "ZIP archive is corrupt or truncated; obtain a complete copy"
    if _cli_truncated_error(chain):
        return "archive is truncated; obtain a complete copy"
    if any(isinstance(error, tarfile.ReadError) for error in chain):
        return "TAR archive has an invalid header or is corrupt; obtain a complete copy"
    return None


def _cli_archive_error(args, exc: BaseException, member: str,
                       chain: Sequence[BaseException]) -> str:
    message = str(exc)
    context = _cli_error_context(args, member)
    if isinstance(exc, ResourceLimitError):
        return f"{context}: {message}; {_cli_limit_advice(message)}"
    if isinstance(exc, UnsafeArchiveError):
        return f"{context}: {message}; use an archive with safe member paths and link targets"
    if isinstance(exc, CreateError):
        return f"{context}: {message}"
    if isinstance(exc, FormatError):
        return f"{context}: {message}; run snug formats to see installed formats"
    if message.startswith("no such archive:"):
        return f"{context}: archive file does not exist; check the path"
    if message.startswith("no such file or directory:"):
        return f"{context}: {message}; check the input path"
    if message.startswith("archive already exists:"):
        return f"{context}: {message}"
    if message == "password file must contain exactly one line":
        return f"{context}, password file {str(args.password_file)!r}: {message}; put one password on one line"
    password_error = _cli_password_error(chain)
    if password_error:
        return f"{context}: {password_error}"
    corruption_error = _cli_corruption_error(chain)
    if corruption_error:
        return f"{context}: {corruption_error}"
    if "py7zr could not process" in message or "libarchive could not read" in message:
        return f"{context}: archive could not be read; obtain a complete copy or check snug formats"
    return f"{context}: {message}"


def _cli_error_text(args, exc: BaseException, member: str = "") -> str:
    """Format CLI failures without changing the shared engine or TUI text."""
    chain = _cli_exception_chain(exc)
    # CreateError carries publication and skipped-input facts that must survive.
    create_policy_error = isinstance(exc, CreateError) and str(exc).startswith(
        ("cannot create archive ", "cannot replace archive "))
    if not create_policy_error:
        for error in chain:
            if isinstance(error, OSError) and error.errno is not None:
                message = _cli_os_error(args, error, member)
                if isinstance(exc, CreateError):
                    message += ("; the existing archive was preserved" if exc.preserved_existing
                                else "; no archive was created")
                return _safe(message)
    return _safe(_cli_archive_error(args, exc, member, chain))


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


def _print_test_summary(report: TestReport) -> None:
    print(f"Tested {_safe(report.archive)}  [{report.format.value}]")
    print(f"  {report.entries} entries, {report.files} files, {human_bytes(report.bytes_read)} decoded")
    print("Archive is OK.")


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

def _emit_cli_create_warning(message: str) -> None:
    """A closed warning consumer cannot undo a published archive's status."""
    try:
        print(message, file=sys.stderr)
        sys.stderr.flush()
    except BrokenPipeError:
        _silence_closed_pipe(sys.stderr)


def _cmd_create(args, engine: ArchiveEngine) -> int:
    archive_path = Path(args.archive)
    if archive_path.exists() and not args.force:
        raise ArchiveError(
            f"archive already exists: {archive_path} (use --force to replace it)"
        )
    display = _CliProgressDisplay("create", quiet=args.quiet, defer_final=True)
    args._cli_progress = display
    password = _read_password(args)
    try:
        try:
            report = engine.create(
                args.archive,
                args.sources,
                fmt=args.format,
                root=args.root,
                compresslevel=args.level,
                symlinks=args.symlinks,
                progress=display,
                password=password,
            )
            display.finish(len(report.skipped) + report.accidental_skips)
        finally:
            display.abort()
    except CreateError as exc:
        _print_create_skips(exc.report, password=password)
        raise
    _print_create_skips(report, password=password, emit=_emit_cli_create_warning)
    if not args.quiet:
        _print_create_summary(report)
        for name in report.skipped:
            _emit_cli_create_warning(f"  skipped: {_safe(name)}")
    if report.accidental_skips:
        _emit_cli_create_warning(
            f"warning: archive is incomplete: {report.included_inputs} included, "
            f"{report.accidental_skips} skipped")
        return 5
    return 0


def _cmd_extract(args, engine: ArchiveEngine) -> None:
    display = _CliProgressDisplay("extract", quiet=args.quiet, defer_final=True)
    args._cli_progress = display
    try:
        report = engine.extract(
            args.archive,
            args.directory,
            members=args.member,
            overwrite=not args.no_overwrite,
            preserve_metadata=not args.no_metadata,
            symlinks=args.symlinks,
            strip_components=args.strip_components,
            limits=ExtractionLimits(args.max_files, args.max_size, args.max_file_size, args.max_ratio),
            progress=display,
            password=_read_password(args),
        )
        display.finish(len(report.skipped))
    finally:
        display.abort()
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


def _cmd_test(args, engine: ArchiveEngine) -> None:
    display = _CliProgressDisplay("test", quiet=args.quiet, defer_final=True)
    args._cli_progress = display
    try:
        report = engine.test(args.archive, password=_read_password(args), progress=display)
        display.finish()
    finally:
        display.abort()
    if not args.quiet:
        _print_test_summary(report)


def _cmd_info(args, engine: ArchiveEngine) -> None:
    data = engine.info(args.archive, password=_read_password(args))
    width = max(len(k) for k in data)
    for key, value in data.items():
        if key in ("archive_size", "uncompressed_size"):
            value = human_bytes(value)
        if isinstance(value, bool):
            value = "yes" if value else "no"
        print(f"{key:<{width}} : {_safe(value)}")


# Diagnostics deliberately do not call runtime activation, repair, or updater APIs.
def _diagnostic_json(path: Path) -> dict:
    import json
    try:
        with path.open("rb") as source:
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return {"status": "corrupt", "message": "JSON file exceeds diagnostic size limit"}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"status": "corrupt", "message": "expected a JSON object"}
        return {"status": "ok", "data": data}
    except FileNotFoundError:
        return {"status": "missing"}
    except (OSError, ValueError, RecursionError) as exc:
        return {"status": "corrupt", "message": _safe(exc)}


def _diagnostic_inventory_name(name: object) -> bool:
    if not isinstance(name, str) or not name:
        return False
    if "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    return not any(part in ("", ".", "..") or ":" in part for part in name.split("/"))


def _diagnostic_digest(value: object) -> bool:
    import re
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


def _diagnostic_checksum(path: Path) -> str:
    import hashlib
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _diagnostic_inventory_member(directory: Path, root: Path, name, expected, hashes: bool) -> dict | None:
    from pathlib import PurePosixPath
    if not _diagnostic_inventory_name(name) or not _diagnostic_digest(expected):
        return {"status": "corrupt", "message": "invalid inventory member"}
    candidate = directory.joinpath(*PurePosixPath(name).parts)
    if not candidate.resolve().is_relative_to(root):
        return {"status": "corrupt", "message": "inventory member escapes its directory"}
    if not candidate.is_file():
        return {"status": "broken", "message": "recorded file is missing"}
    if hashes and _diagnostic_checksum(candidate) != expected.lower():
        return {"status": "broken", "message": "recorded file checksum mismatch"}
    return None


def _diagnostic_inventory(directory: Path, *, hashes: bool) -> dict:
    if not directory.exists():
        return {"status": "missing", "verification": "sha256" if hashes else "presence"}
    record = _diagnostic_json(directory / ".snug-files.json")
    result = {"status": record["status"], "verification": "sha256" if hashes else "presence"}
    if record["status"] != "ok":
        return result
    files = record["data"]
    if not files:
        return {**result, "status": "corrupt", "message": "empty file inventory"}
    try:
        root = directory.resolve()
        if not (directory / ".snug-files.json").resolve().is_relative_to(root):
            return {**result, "status": "corrupt", "message": "inventory escapes its directory"}
        for name, expected in files.items():
            error = _diagnostic_inventory_member(directory, root, name, expected, hashes)
            if error is not None:
                return {**result, **error}
    except (OSError, ValueError) as exc:
        return {**result, "status": "broken", "message": _safe(exc)}
    return {**result, "files": len(files)}


def _diagnostic_registration(ffi, kind: str, name: str) -> bool:
    """Require a successful registration on a fresh native archive handle."""
    import ctypes
    import re
    if not re.fullmatch(r"[a-z0-9_]{1,64}", name) or name == "all":
        return False
    prefix, new, free = {
        "reader": ("read_support_format_", "read_new", "read_free"),
        "filter": ("read_support_filter_", "read_new", "read_free"),
        "writer": ("write_set_format_", "write_new", "write_free"),
    }[kind]
    handle = None
    try:
        native = getattr(ffi, "libarchive", None)
        if native is None:
            probe = ffi.ffi(prefix + name, [ffi.c_archive_p], ffi.c_int)
        else:
            # A fresh callable avoids the binding's warning logger and leaves
            # its shared errcheck handlers untouched for archive operations.
            probe = ctypes.CFUNCTYPE(ffi.c_int, ffi.c_archive_p)(("archive_" + prefix + name, native))
        handle = getattr(ffi, new)()
        return bool(handle) and probe(handle) == 0
    except Exception:
        return False
    finally:
        if handle:
            getattr(ffi, free)(handle)


def _diagnostic_aes_available() -> bool:
    import importlib
    try:
        importlib.import_module("Cryptodome.Cipher.AES").new(bytes(32), 1)
        return True
    except Exception:
        return False


def _diagnostic_py7zr(backend) -> dict:
    import importlib
    import re
    library = getattr(backend, "_library")()
    version = str(getattr(library, "__version__", "unknown"))
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:$|[.a-zA-Z+-])", version)
    supported = bool(match and (1, 1, 3) <= tuple(map(int, match.groups())) < (2,))
    api = callable(getattr(library, "SevenZipFile", None)) and callable(getattr(library, "is_7zfile", None))
    writer = hasattr(importlib.import_module("py7zr.io"), "WriterFactory")
    aes = _diagnostic_aes_available()
    result = {"version": version, "compatible": supported,
              "required_api": api and writer, "aes": aes,
              "available": supported and api and writer and aes}
    if not result["available"]:
        result.update(status="broken", message="py7zr version, streaming API, or AES support is unusable")
    return result


def _diagnostic_native_capabilities(ffi, kind: str, names) -> list[str]:
    candidates = {str(value) for value in names}
    if kind == "reader":
        candidates.add("rar5")
    return [name for name in sorted(candidates) if _diagnostic_registration(ffi, kind, name)]


def _diagnostic_libarchive(backend) -> dict:
    import importlib.metadata
    library = getattr(backend, "_library")()
    ffi = library.ffi
    native = int(ffi.version_number())
    # Binding sets establish candidates, not successful native registrations.
    readers = _diagnostic_native_capabilities(ffi, "reader", ffi.READ_FORMATS)
    writers = _diagnostic_native_capabilities(ffi, "writer", ffi.WRITE_FORMATS)
    filters = _diagnostic_native_capabilities(ffi, "filter", getattr(ffi, "READ_FILTERS", ()))
    api = callable(getattr(library, "file_reader", None)) and callable(getattr(library, "file_writer", None))
    try:
        binding = importlib.metadata.version("libarchive-c")
    except importlib.metadata.PackageNotFoundError:
        binding = str(getattr(library, "__version__", "unknown"))
    required_readers = {"7zip", "ar", "cab", "cpio", "iso9660", "lha", "rar", "xar", "warc", "zip"}
    managed = native >= 3008000 and required_readers <= set(readers) and {"ar_bsd", "cpio_newc"} <= set(writers)
    result = {"binding_version": binding, "native_version": native,
              "native_version_string": f"{native // 1000000}.{native // 1000 % 1000}.{native % 1000}",
              "required_api": api, "managed_compatible": managed,
              "native_readers": readers, "native_writers": writers,
              "native_filters": filters, "available": api}
    if not api:
        result.update(status="broken", message="libarchive streaming API is unusable")
    return result


def _diagnostic_writable_formats(backend, component: dict) -> list[str]:
    return sorted(fmt.value for fmt in backend.write_formats if backend.can_write(fmt)
                  and (backend.name != "libarchive"
                       or getattr(backend, "_write_names", {}).get(fmt) in component["native_writers"]))


def _diagnostic_backend(backend) -> dict:
    component = {"name": str(backend.name), "status": "ok", "available": False,
                 "read_formats": sorted(fmt.value for fmt in backend.read_formats),
                 "write_formats": sorted(fmt.value for fmt in backend.write_formats)}
    try:
        if not backend.available():
            component.update(status="unavailable", message=f"{backend.name} unavailable")
        elif backend.name == "py7zr":
            component.update(_diagnostic_py7zr(backend))
        elif backend.name == "libarchive":
            component.update(_diagnostic_libarchive(backend))
        else:
            component["available"] = True
        if component["available"]:
            component["write_formats"] = _diagnostic_writable_formats(backend, component)
    except Exception as exc:
        component.update(status="broken", available=False, message=_safe(exc))
    return component


def _diagnostic_backends(backends=None) -> list[dict]:
    from snug_core import _backends
    library_override = os.environ.get("LIBARCHIVE")
    try:
        available = list(backends) if backends is not None else _backends()
        return [_diagnostic_backend(backend) for backend in available]
    finally:
        if library_override is None:
            os.environ.pop("LIBARCHIVE", None)
        else:
            os.environ["LIBARCHIVE"] = library_override


def _diagnostic_can_read(component: dict, fmt: ArchiveFormat) -> bool:
    if component["name"] != "libarchive":
        return True
    # Reader-name aliases, not a separate archive capability matrix.
    aliases = {"7z": "7zip", "iso": "iso9660", "zipx": "zip", "lzh": "lha", "deb": "ar", "rpm": "cpio"}
    if aliases.get(fmt.value, fmt.value) not in component["native_readers"]:
        return False
    return fmt.value != "rpm" or "rpm" in component["native_filters"]


def _diagnostic_format_support(component: dict, fmt: ArchiveFormat) -> tuple[bool, bool, str | None]:
    declares_read = fmt.value in component["read_formats"]
    declares_write = fmt.value in component["write_formats"]
    if not (declares_read or declares_write):
        return False, False, None
    if not component["available"]:
        return False, False, f"{component['name']} {component['status']}"
    readable = declares_read and _diagnostic_can_read(component, fmt)
    message = None
    if declares_read and not readable:
        message = f"{component['name']} reader unavailable"
    return readable, declares_write, message


def _diagnostic_format_status(readers: list[str], writers: list[str]) -> str:
    if readers and writers:
        return "read/write"
    if readers:
        return "read only"
    if writers:
        return "write only"
    return "unavailable"


def _diagnostic_format_row(fmt: ArchiveFormat, components: list[dict]) -> dict:
    readers, writers, unavailable = [], [], []
    for component in components:
        readable, writable, message = _diagnostic_format_support(component, fmt)
        if readable:
            readers.append(component["name"])
        if writable:
            writers.append(component["name"])
        if message is not None:
            unavailable.append(message)
    if fmt.value == "zipx" and "native" in readers:
        unavailable.append("native ZIPX reading supports only this Python's ZIP codecs; other methods require libarchive")
    return {"format": fmt.value, "read": bool(readers), "write": bool(writers),
            "read_backends": readers, "write_backends": writers,
            "status": _diagnostic_format_status(readers, writers), "details": unavailable}


def _formats_report(backends=None) -> dict:
    components = _diagnostic_backends(backends)
    rows = [_diagnostic_format_row(fmt, components) for fmt in ArchiveFormat]
    return {"schema_version": 1, "formats": rows,
            "note": "Capabilities describe installed backends and registered readers; individual codecs, encryption, and archive variants can still be unsupported."}


def _diagnostic_runtime_lock(root: Path) -> dict:
    lock = _diagnostic_json(root / "runtime-lock.json")
    if lock["status"] != "ok":
        return lock
    schema = lock["data"].get("schema")
    binding = lock["data"].get("binding", {})
    if type(schema) is not int or schema != 1 or not _diagnostic_binding_valid(binding):
        return {"status": "corrupt", "message": "invalid runtime lock schema"}
    return lock


def _diagnostic_binding_valid(binding) -> bool:
    if not isinstance(binding, dict):
        return False
    filename, url = binding.get("filename"), binding.get("url")
    return (isinstance(filename, str) and bool(filename)
            and isinstance(url, str) and url.startswith("https://")
            and _diagnostic_digest(binding.get("sha256")))


def _diagnostic_runtime_state(root: Path) -> dict:
    state = _diagnostic_json(root / "runtime.json")
    if state["status"] == "ok" and state["data"].get("kind") not in ("homebrew", "windows"):
        return {"status": "corrupt", "message": "invalid runtime state kind"}
    return state


def _diagnostic_managed_root(root: Path) -> bool:
    configured = os.environ.get("SNUG_MANAGED_ROOT")
    try:
        return bool(configured and Path(configured).resolve() == root)
    except (OSError, ValueError):
        return False


def _diagnostic_installation_kind(source: bool, pip: bool, windows: bool, homebrew: bool) -> str:
    if source:
        return "source"
    if pip:
        return "pip"
    if windows:
        return "windows"
    if homebrew:
        return "homebrew"
    return "external"


def _diagnostic_installation(root: Path, state: dict, marker: dict) -> dict:
    marker_data = marker.get("data", {})
    state_data = state.get("data", {})
    source = any((parent / ".git").exists() for parent in (root, *root.parents))
    pip = bool(list(root.glob("*.dist-info")) or list(root.glob("*.egg-info")))
    identified = _diagnostic_managed_root(root)
    homebrew = (type(marker_data.get("schema")) is int and marker_data.get("schema") == 1
                and marker_data.get("kind") == "homebrew" and marker_data.get("branch") == "main"
                and not (root / ".snug-install.json").is_symlink())
    windows = ((state["status"] == "ok" and state_data.get("kind") == "windows")
               or ((root / "runtime.ps1").is_file() and (root / "runtime.json").exists())
               or (identified and sys.platform == "win32"))
    managed = not (source or pip) and (identified or windows)
    updater_owned = managed and homebrew and sys.platform != "win32"
    return {"managed": managed, "windows": windows, "updater_owned": updater_owned,
            "kind": _diagnostic_installation_kind(source, pip, windows, homebrew)}


def _diagnostic_backend_health(components: list[dict], managed: bool) -> bool:
    healthy = sys.version_info >= (3, 10)
    for component in components:
        required = managed or component["name"] in ("native", "native-stream")
        component["required"] = required
        if required and (not component["available"] or (component["name"] == "libarchive" and not component.get("managed_compatible", True))):
            healthy = False
    return healthy


def _diagnostic_runtime_health(root: Path, installation: dict, state: dict, lock: dict, integrity: dict) -> bool:
    if not installation["managed"]:
        return True
    healthy = lock["status"] == "ok" and integrity["vendor"]["status"] == "ok"
    if installation["windows"] and (state["status"] != "ok" or state.get("data", {}).get("kind") != "windows"):
        healthy = False
    for name in ("packages", "native"):
        if (root / name).exists() and integrity[name]["status"] != "ok":
            healthy = False
    return healthy


def _doctor_report(root: Path | None = None, backends=None) -> dict:
    import platform
    root = Path(__file__).resolve().parent if root is None else Path(root).resolve()
    state = _diagnostic_runtime_state(root)
    lock = _diagnostic_runtime_lock(root)
    marker = _diagnostic_json(root / ".snug-install.json")
    installation = _diagnostic_installation(root, state, marker)
    integrity = {name: _diagnostic_inventory(root / name, hashes=name == "vendor")
                 for name in ("vendor", "packages", "native")}
    components = _diagnostic_backends(backends)
    backend_health = _diagnostic_backend_health(components, installation["managed"])
    runtime_health = _diagnostic_runtime_health(root, installation, state, lock, integrity)
    updater_owned = installation["updater_owned"]
    for description in (state, lock, marker):
        description.pop("data", None)
    return {"schema_version": 1, "ok": backend_health and runtime_health, "snug_version": __version__,
            "python": {"version": platform.python_version(), "executable": sys.executable, "supported": sys.version_info >= (3, 10)},
            "platform": {"system": platform.system(), "architecture": platform.machine()},
            "backends": components,
            "runtime": {"managed": installation["managed"], "kind": installation["kind"],
                        "state": state, "lock": lock, "integrity": integrity},
            "updater": {"ownership": "managed" if updater_owned else "external", "marker": marker,
                        "reason": "installer-owned main Homebrew installation" if updater_owned else "update using the original installation method"}}


def _print_doctor_backend(component: dict) -> None:
    requirement = "required" if component["required"] else "optional"
    detail = component.get("message", component.get("version", component.get("native_version_string", "")))
    print(f"{_safe(component['name'])}: {component['status']} ({requirement}) {_safe(detail)}".rstrip())
    if component["name"] == "py7zr" and "required_api" in component:
        print(f"  required API: {component['required_api']}; AES: {component['aes']}")
    if component["name"] == "libarchive" and "native_readers" in component:
        print(f"  binding: {_safe(component['binding_version'])}; managed-compatible: {component['managed_compatible']}")
        print(f"  readers: {_safe(', '.join(component['native_readers']))}")
        print(f"  writers: {_safe(', '.join(component['native_writers']))}")


def _print_doctor_report(report: dict) -> None:
    print(f"Snug {report['snug_version']}  Python {report['python']['version']}")
    print(f"Platform: {_safe(report['platform']['system'])} {_safe(report['platform']['architecture'])}")
    for component in report["backends"]:
        _print_doctor_backend(component)
    runtime = report["runtime"]
    print(f"Runtime: {runtime['kind']}; state: {runtime['state']['status']}; lock: {runtime['lock']['status']}")
    for name, inventory in runtime["integrity"].items():
        print(f"  {name}: {inventory['status']} ({inventory['verification']})")
    print(f"Updater installation: {report['updater']['ownership']}")
    print("Required components are healthy." if report["ok"] else "Required components need attention.")


def _cmd_doctor(args) -> int:
    import json
    report = _doctor_report()
    if args.json:
        print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    else:
        _print_doctor_report(report)
    return 0 if report["ok"] else 2


def _print_format_row(row: dict) -> None:
    names = list(dict.fromkeys([*row["read_backends"], *row["write_backends"]]))
    status = ", ".join(names) if names else "; ".join(row["details"]) or "unavailable"
    if names and row["details"]:
        status += "; " + "; ".join(row["details"])
    print(f"{row['format']:<9} {'yes' if row['read'] else 'no':<5} {'yes' if row['write'] else 'no':<5} {_safe(status)}")


def _cmd_formats(args) -> int:
    import json
    report = _formats_report()
    if args.json:
        print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    else:
        print(f"{'Format':<9} {'Read':<5} {'Write':<5} Backend / status")
        for row in report["formats"]:
            _print_format_row(row)
        print(report["note"])
    return 0


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
        print(_INTERRUPTED_MESSAGE, file=sys.stderr)
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


def _run_interactive_cli() -> int:
    handle = _start_update_check() if sys.stdin.isatty() and sys.stdout.isatty() else None
    code = _launch_interactive()
    if code == 0:
        _update_notice(handle)
    return code


def _emit_cli_stderr(message: str) -> None:
    """A closed error consumer must not turn a failed report into success."""
    try:
        print(message, file=sys.stderr)
    except BrokenPipeError:
        _silence_closed_pipe(sys.stderr)
        # Preserve the existing failed-error-write status, outside stdout policy.
        raise SystemExit(1) from None


def _run_diagnostic_command(args) -> int:
    dispatch = {"doctor": _cmd_doctor, "formats": _cmd_formats}
    try:
        return dispatch[args.command](args)
    except BrokenPipeError:
        raise
    except (ArchiveError, OSError, ValueError) as exc:
        _emit_cli_stderr(f"error: {_safe(exc)}")
        return 2
    except KeyboardInterrupt:
        _emit_cli_stderr(_INTERRUPTED_MESSAGE)
        return 130


def _report_cli_archive_error(args, exc: Exception) -> int:
    display = getattr(args, "_cli_progress", None)
    member = display.current if display is not None else ""
    prefix = "unsafe archive: " if isinstance(exc, UnsafeArchiveError) else ""
    _emit_cli_stderr(f"error: {prefix}{_cli_error_text(args, exc, member)}")
    if isinstance(exc, ResourceLimitError):
        return 4
    return 3 if isinstance(exc, UnsafeArchiveError) else 2


def _run_archive_command(args) -> int:
    handle = _start_update_check()
    engine = ArchiveEngine()

    dispatch = {
        "create": _cmd_create,
        "extract": _cmd_extract,
        "list": _cmd_list,
        "info": _cmd_info,
        "test": _cmd_test,
    }

    try:
        status = dispatch[args.command](args, engine)
    except (ArchiveError, OSError, zipfile.BadZipFile, tarfile.TarError,
            EOFError, ValueError) as exc:
        if isinstance(exc, BrokenPipeError) and args.command in {"list", "info"}:
            raise
        return _report_cli_archive_error(args, exc)
    except KeyboardInterrupt:
        _emit_cli_stderr(_INTERRUPTED_MESSAGE)
        return 130

    if status in (None, 0) and args.command in {"create", "extract"} and not args.quiet:
        _update_notice(handle)
    return 0 if status is None else status


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv:
        return _run_interactive_cli()
    args = _build_parser().parse_args(argv)
    if args.command == "update":
        return _cmd_update(args)
    if args.command in {"doctor", "formats"}:
        return _run_with_closed_stdout(lambda: _run_diagnostic_command(args))
    if args.command in {"list", "info"}:
        return _run_with_closed_stdout(lambda: _run_archive_command(args))
    return _run_archive_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
