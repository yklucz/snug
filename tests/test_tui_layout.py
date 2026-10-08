"""Bounded TUI rendering and deterministic idle-resize checks without a TTY."""

from contextlib import nullcontext
import io
import re
from types import SimpleNamespace
import unicodedata

import pytest

import snug
import snug_core
from snug_core import ArchiveInspection


SIZES = [(80, 24), (80, 16), (60, 12), (40, 10),
         (39, 10), (40, 9), (20, 8), (1, 1)]
CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")
SGR = re.compile(r"\x1b\[[0-9;]*m")


def _cells(text):
    """Independent cell estimate for assertions, including zero-width marks."""
    total = 0
    for char in SGR.sub("", text):
        if unicodedata.combining(char) or unicodedata.category(char) in (
                "Mn", "Me", "Cf", "Cc", "Cs"):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def _terminal_rows(output, size):
    """Interpret cursor/clear operations and reject writes beyond the grid."""
    cols, height = size
    row = col = 1
    lines = {}
    writes = set()
    coordinates = []
    offset = 0
    while offset < len(output):
        escape = CSI.match(output, offset)
        if escape:
            parameters, _, action = escape.groups()
            if action in ("H", "f"):
                values = parameters.split(";")
                row = int(values[0] or 1)
                col = int(values[1] or 1) if len(values) > 1 else 1
                assert 1 <= row <= height
                assert 1 <= col <= cols
                coordinates.append((row, col))
            elif action == "J":
                lines.clear()
                writes.clear()
            elif action == "K" and col == 1:
                lines.pop(row, None)
            offset = escape.end()
            continue
        char = output[offset]
        assert char != "\x1b", "unparsed terminal escape"
        if char == "\r":
            col = 1
        elif char == "\n":
            row += 1
            assert row <= height, "newline scrolls past the bottom row"
        else:
            width = _cells(char)
            assert 1 <= row <= height
            if width:
                assert 1 <= col <= cols
                assert col + width - 1 <= cols, "wide character crosses the right edge"
                for occupied_col in range(col, col + width):
                    cell = (row, occupied_col)
                    assert cell not in writes, "body and footer overwrite the same cell"
                    writes.add(cell)
                col += width
            lines[row] = lines.get(row, "") + char
        offset += 1
    assert coordinates, "no screen was rendered"
    assert len(lines) <= height
    assert all(_cells(line) <= cols for line in lines.values())
    return lines


@pytest.fixture(autouse=True)
def _isolated_result_lines(monkeypatch):
    monkeypatch.setattr(snug, "_TUI_LAST_LINES", [])


def _screen(kind, tmp_path, monkeypatch):
    if kind.startswith("picker"):
        if kind != "picker-empty":
            for index in range(30):
                (tmp_path / f"item-{index:02d}-界🙂e\u0301.txt").touch()
        screen = snug._PickerScreen(tmp_path)
        if screen.entries:
            screen.cursor = 23
        if kind == "picker-marked":
            screen.marked.update(screen.entries[:2])
        return screen
    if kind == "main":
        screen = snug._MenuScreen("Select an action", str(tmp_path), snug._MENU_OPTIONS)
        screen.selected = 4
        return screen
    if kind == "subtitle":
        options = snug._ExtractOptions(tmp_path / "archive.zip", False).menu_options()
        screen = snug._MenuScreen("Extract archive", "界🙂e\u0301" * 50, options)
        screen.selected = 6
        return screen
    if kind == "pause":
        return snug._PauseScreen(["error: unsafe archive: path traversal"]
                                 + [f"detail-{i}: 界🙂e\u0301" * 20 for i in range(30)])

    captured = []

    def capture(title, options, subtitle=None):
        captured.append(snug._MenuScreen(title, subtitle, options))
        return None

    monkeypatch.setattr(snug, "_select_menu", capture)
    if kind == "members":
        engine = SimpleNamespace(inspect=lambda *a, **k: ArchiveInspection(
            [snug.ArchiveEntry(f"member-{i:02d}-界🙂e\u0301.txt") for i in range(30)], {}))
        snug._menu_members(engine, tmp_path / "archive.zip", None, None)
        captured[0].selected = 23
    else:
        assert kind == "limits"
        snug._menu_limits(snug.ExtractionLimits())
        captured[0].selected = 2
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("kind", ["main", "picker", "picker-marked", "picker-empty",
                                 "subtitle", "members", "limits", "pause"])
def test_every_screen_obeys_row_column_and_footer_budgets(monkeypatch, tmp_path, kind, size):
    screen = _screen(kind, tmp_path, monkeypatch)
    output = io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", output)
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    body = screen.draw(*size)
    footer = screen.footer()
    snug._draw_lines(body, footer=footer, size=size)
    rendered = _terminal_rows(output.getvalue(), size)
    visible = "\n".join(rendered.values())

    if size[0] < 40 or size[1] < 10:
        assert footer == []
        if size != (1, 1):
            assert "Terminal too small" in visible
            assert "40x10" in visible
        else:
            assert rendered[1].strip()
        return

    assert len(body) + len(footer) <= size[1]
    assert len(footer) <= (3 if kind.startswith("picker") else 2)
    if isinstance(screen, snug._MenuScreen):
        key, label = screen.options[screen.selected]
        assert any("❯" in line and f"{key})" in line and label[:10] in line
                   for line in rendered.values())
    elif isinstance(screen, snug._PickerScreen):
        if screen.entries:
            selected = screen._visible()[screen.cursor].name
            assert any("❯" in line and selected in line for line in rendered.values())
        else:
            assert "empty directory" in visible
        assert f"Marked ({len(screen.marked)})" in visible
        if size == (80, 24):
            assert "filter" in visible.lower()
    else:
        assert "error: unsafe archive" in visible
        assert "Enter" in visible

    if size != (80, 24):
        assert len(footer) == 1
        if kind.startswith("picker"):
            assert "Confirm" in visible and "Cancel" in visible
        elif kind in ("subtitle", "members"):
            assert "Back" in visible
            assert ("Extract archive" if kind == "subtitle" else "Use this selection") in visible
        elif kind == "limits":
            assert "Use these limits" in visible and "Esc" in visible
        elif kind == "main":
            assert "Quit" in visible


@pytest.mark.parametrize("size", SIZES)
def test_cursor_coordinates_are_clamped_in_both_dimensions(size):
    for row, col in [(0, 0), (-50, -50), (9999, 9999), (1, 1)]:
        match = CSI.fullmatch(snug._cursor_at(row, col, size=size))
        assert match is not None
        actual_row, actual_col = map(int, match.group(1).split(";"))
        assert actual_row == max(1, min(row, size[1]))
        assert actual_col == max(1, min(col, size[0]))


@pytest.mark.parametrize("size", SIZES)
def test_shared_renderer_clips_oversized_body_and_footer_together(monkeypatch, size):
    output = io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", output)
    body = [f"body-{i:02d}-界🙂" * 30 for i in range(100)]
    footer = ["footer-confirm", "footer-cancel"]
    snug._draw_lines(body, footer=footer, size=size)
    rendered = _terminal_rows(output.getvalue(), size)
    assert len(rendered) <= size[1]
    if size[0] >= 20:
        assert "footer-confirm" in rendered[size[1] - 1]
        assert "footer-cancel" in rendered[size[1]]
        assert "body-00" in rendered[1]
        assert not any(f"body-{size[1] - 2:02d}" in line for line in rendered.values())
    else:
        assert rendered[1] == "…"


@pytest.mark.parametrize("size, expected", [
    ((80, 24), "full"), ((100, 30), "full"), ((79, 24), "compact"),
    ((80, 23), "compact"), ((40, 10), "compact"), ((39, 24), "small"),
    ((80, 9), "small"), ((1, 1), "small"),
])
def test_size_mode_uses_both_thresholds(size, expected):
    assert snug._size_mode(*size) == expected


def _loop(monkeypatch, screen, events, initial_size):
    current_size = [initial_size]
    frames = []
    waits = []
    scripted = iter(events)
    monkeypatch.setattr(snug, "_raw_mode", nullcontext)
    monkeypatch.setattr(snug, "_term_size", lambda: current_size[0])
    monkeypatch.setattr(snug.sys, "stdout", io.StringIO())
    renderer = snug._draw_lines

    def draw(lines, footer=None, *, size=None):
        frames.append((size, list(lines), list(footer or [])))
        renderer(lines, footer=footer, size=size)

    def read(timeout):
        waits.append(timeout)
        event = next(scripted)
        if callable(event):
            return event(current_size)
        if isinstance(event, BaseException):
            raise event
        return event

    monkeypatch.setattr(snug, "_draw_lines", draw)
    monkeypatch.setattr(snug, "_read_key_timeout", read)
    snug._run_screen(screen)
    assert waits and all(0 < timeout <= 0.2 for timeout in waits)
    return frames, waits


@pytest.mark.parametrize("key", ["enter", "q", "space", "down", "up", "right", "/", "界"])
def test_too_small_ignores_keys_without_mutating_picker(monkeypatch, tmp_path, key):
    source = tmp_path / "source.txt"
    source.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.marked.add(source)
    handled = []
    handler = screen.handle

    def handle(event):
        handled.append(event)
        return handler(event)

    monkeypatch.setattr(screen, "handle", handle)
    frames, _ = _loop(monkeypatch, screen, [key, None, "esc"], (20, 8))
    assert handled == ["esc"]
    assert screen.result is None
    assert screen.marked == {source}
    assert screen.current == tmp_path and screen.filter == ""
    assert len(frames) == 1


def test_too_small_escape_retains_picker_filter_clear_then_cancel(monkeypatch, tmp_path):
    source = tmp_path / "keep.txt"
    source.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.marked.add(source)
    screen.handle("k")

    def after_clear(_):
        assert screen.filter == "" and not screen.filter_mode
        assert screen.marked == {source}
        assert screen.result is None
        return "esc"

    frames, _ = _loop(monkeypatch, screen, ["esc", after_clear], (20, 8))
    assert len(frames) == 2
    assert screen.result is None and screen.marked == {source}


@pytest.mark.parametrize("key", ["/", "k"], ids=["empty-filter-mode", "active-filter"])
def test_compact_picker_footer_describes_filter_space_and_escape(monkeypatch, tmp_path, key):
    source = tmp_path / "keep.txt"
    source.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.marked.add(source)
    screen.handle(key)
    screen.draw(40, 10)
    footer = "\n".join(screen.footer())
    assert "Esc Clear/Cancel" in footer
    assert "Space Mark" not in footer
    assert "Enter Confirm" in footer
    assert _cells(footer) <= 39
    screen.draw(80, 24)
    assert "Space text" in "\n".join(screen.footer())
    assert screen.marked == {source}


@pytest.mark.parametrize("size", [(80, 24), (20, 8)])
def test_ctrl_c_propagates_and_screen_state_is_retained(monkeypatch, tmp_path, size):
    source = tmp_path / "keep.txt"
    source.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.marked.add(source)
    with pytest.raises(KeyboardInterrupt):
        _loop(monkeypatch, screen, [KeyboardInterrupt()], size)
    assert screen.marked == {source}
    assert screen.result is None


def test_idle_resize_recovers_picker_without_a_key_and_without_redundant_redraws(monkeypatch, tmp_path):
    screen = _screen("picker-marked", tmp_path, monkeypatch)
    original = (screen.cursor, screen.current, set(screen.marked), screen.filter)

    def resize(size):
        def event(current_size):
            assert (screen.cursor, screen.current, screen.marked, screen.filter) == original
            current_size[0] = size
            return None
        return event

    frames, _ = _loop(monkeypatch, screen,
                      [None, resize((20, 8)), None, "enter", "space", "q",
                       resize((60, 12)), None, "esc"], (80, 24))
    assert [frame[0] for frame in frames] == [(80, 24), (20, 8), (60, 12)]
    assert "Terminal too small" in "\n".join(frames[1][1])
    assert any(screen.entries[original[0]].name in line for line in frames[2][1])
    assert (screen.cursor, screen.current, screen.marked, screen.filter) == original
    assert screen.result is None


def test_resize_during_input_poll_blocks_enter_before_menu_activation(monkeypatch):
    screen = snug._MenuScreen("Choose", None, [("r", "Extract archive"), ("b", "Back")])

    def shrink(current_size):
        current_size[0] = (20, 8)
        return "enter"

    frames, _ = _loop(monkeypatch, screen, [shrink, "esc"], (80, 24))
    assert screen.result is None
    assert [frame[0] for frame in frames] == [(80, 24), (20, 8)]


@pytest.mark.parametrize("key", ["r", "b"])
def test_compact_members_keeps_selected_apply_and_back_visible(monkeypatch, tmp_path, key):
    screen = _screen("members", tmp_path, monkeypatch)
    screen.selected = next(i for i, option in enumerate(screen.options) if option[0] == key)
    output = io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", output)
    snug._draw_lines(screen.draw(40, 10), footer=screen.footer(), size=(40, 10))
    rendered = _terminal_rows(output.getvalue(), (40, 10))
    label = screen.options[screen.selected][1]
    assert any("❯" in line and label in line for line in rendered.values())
    assert "Use this selection" in "\n".join(rendered.values())
    assert "Back" in "\n".join(rendered.values())
    assert screen.handle("enter") is snug._EXIT
    assert screen.result == key


def test_idle_resize_preserves_active_filter_and_marks_until_escape(monkeypatch, tmp_path):
    screen = _screen("picker-marked", tmp_path, monkeypatch)
    screen.handle("i")
    screen.cursor = 17
    original = (screen.cursor, screen.current, set(screen.marked), screen.filter, screen.filter_mode)

    def resize(size):
        def event(current_size):
            assert (screen.cursor, screen.current, screen.marked, screen.filter, screen.filter_mode) == original
            current_size[0] = size
            return None
        return event

    frames, _ = _loop(monkeypatch, screen,
                      [resize((20, 8)), "enter", "space", "q", resize((40, 10)),
                       None, "esc", "esc"], (80, 24))
    assert [frame[0] for frame in frames] == [(80, 24), (20, 8), (40, 10), (40, 10)]
    assert "Filter: i" in frames[2][1][0]
    assert any(screen.entries[17].name in line for line in frames[2][1])
    assert "Clear/Cancel" in frames[2][2][0]
    assert screen.current == original[1] and screen.marked == original[2]
    assert screen.filter == "" and not screen.filter_mode and screen.result is None


@pytest.mark.parametrize("text, expected", [
    ("ASCII", 5), ("界漢", 4), ("🙂🎉", 4), ("e\u0301", 1),
    ("Việt", 4), ("界🙂e\u0301", 5), ("\x00\n\t\u200d\ufe0f", 0),
    ("\x1b[31m界🙂e\u0301\x1b[0m", 5),
])
def test_tui_cell_width_handles_wide_combining_control_and_sgr_text(text, expected):
    assert snug._tui_cell_width(text) == expected


@pytest.mark.parametrize("text", ["界漢字" * 8, "🙂🎉" * 8, "e\u0301" * 16])
@pytest.mark.parametrize("width", [0, 1, 2, 3, 5, 10])
def test_tui_truncation_preserves_whole_unicode_characters_and_cell_budget(text, width):
    result = snug._tui_truncate(text, width)
    assert _cells(result) <= width
    assert result.encode("utf-8").decode("utf-8") == result
    prefix = result.removesuffix("…")
    assert text.startswith(prefix)
    if width == 0:
        assert result == ""
    else:
        assert result.endswith("…")
        if "\u0301" in text and prefix:
            assert prefix.endswith("\u0301"), "truncation leaves a retained base without its mark"


def test_tui_truncation_keeps_styling_and_exact_fit_text():
    styled = "\x1b[31m界🙂e\u0301\x1b[0m"
    assert snug._tui_truncate(styled, 5) == styled
    truncated = snug._tui_truncate(styled, 3)
    assert _cells(truncated) <= 3
    assert SGR.sub("", truncated) == "界…"
    assert truncated.startswith("\x1b[31m") and truncated.endswith("\x1b[0m")


@pytest.mark.parametrize("text, width, visible_length, expected", [
    ("abcdef", 4, 6, "abc…"), ("界界界", 2, 3, "界…"),
    ("e\u0301x", 2, 3, "e…"), ("\x1b[31m界界界\x1b[0m", 2, 3, "\x1b[31m界…"),
])
def test_shared_cli_width_helpers_keep_existing_codepoint_outputs(
        monkeypatch, text, width, visible_length, expected):
    monkeypatch.setattr(snug_core, "_COLOR_ENABLED", False)
    assert snug_core._visible_len(text) == visible_length
    assert snug_core._truncate(text, width).encode("utf-8") == expected.encode("utf-8")
    assert snug._visible_len(text) == visible_length
    assert snug._truncate(text, width).encode("utf-8") == expected.encode("utf-8")


class _TTYOutput(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("unsafe", [False, True], ids=["list-results", "unsafe-error"])
def test_tty_results_and_errors_reach_bounded_pause(monkeypatch, tmp_path, unsafe):
    size = (80, 16)
    output = _TTYOutput()
    monkeypatch.setattr(snug.sys, "stdout", output)
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(snug, "_raw_mode", nullcontext)
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    keys = iter([None, "esc", "enter"])
    monkeypatch.setattr(snug, "_read_key_timeout", lambda timeout: next(keys))
    monkeypatch.setattr(snug, "_select_archive", lambda: tmp_path / "archive.zip")
    entries = [snug.ArchiveEntry(f"item-{i:02d}-界🙂e\u0301.txt", size=100)
               for i in range(30)]
    engine = SimpleNamespace(list_entries=lambda *a, **k: entries)

    def reject(_):
        raise snug.UnsafeArchiveError("path traversal: " + "界🙂" * 100)

    assert snug._run_menu_handler(engine, reject if unsafe else snug._menu_list) is None
    rendered = _terminal_rows(output.getvalue(), size)
    visible = "\n".join(rendered.values())
    assert "Enter" in visible
    if unsafe:
        assert "error: unsafe archive" in visible
    else:
        assert "item-00" in visible
        assert "item-29" not in visible


def test_non_tty_result_context_keeps_exact_stdout_and_stderr(monkeypatch):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", stdout)
    monkeypatch.setattr(snug.sys, "stderr", stderr)
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with snug._menu_result_output():
        print("  界🙂e\u0301 CLI result")
        print("  warning", file=snug.sys.stderr)
    assert stdout.getvalue() == "  界🙂e\u0301 CLI result\n"
    assert stderr.getvalue() == "  warning\n"
    assert snug._TUI_LAST_LINES == []
