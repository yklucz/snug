"""TTY interrupts leave one screen level; raw EOF and plain input stay distinct."""

from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import snug
from test_tui_editor import tty_prompt
from test_tui_input import unix_input
from test_tui_layout import _terminal_rows
from test_tui_options import make_zip


SIZES = [(80, 24), (40, 10), (20, 8)]
CANCELS = ["esc", KeyboardInterrupt()]


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("cancel", CANCELS, ids=["Escape", "Ctrl-C"])
@pytest.mark.parametrize("kind", ["members", "limits", "field", "invalid-field", "options", "result", "error"])
def test_nested_screens_apply_the_same_escape_effects_without_exiting_session(
        tty_prompt, monkeypatch, size, cancel, kind):
    original = snug.ExtractionLimits(3, 4000, 2000, 8)
    if kind == "members":
        screen = snug._MembersScreen(["one", "two"], ["one"])
        screen.handle("n")
        screen.handle("2")
    elif kind in ("limits", "field", "invalid-field"):
        screen = snug._LimitsScreen(original)
        for key in ("s", "9", "enter"):
            screen.handle(key)
        if kind != "limits":
            screen.handle("e")
            for key in ("-1" if kind == "invalid-field" else "99"):
                screen.handle(key)
            if kind == "invalid-field":
                screen.handle("enter")
                assert screen._notice
    elif kind == "options":
        screen = snug._MenuScreen("Options", None, [("r", "Run"), ("b", "Back")])
        screen.handle("down")
    else:
        screen = snug._PauseScreen(["error: safe failure" if kind == "error" else "completed result"])
    checked = []

    def after_field(output):
        assert screen.editing is None and screen.buffer == "" and screen._notice is None
        assert screen.values["max_entries"] == 3
        assert screen.values["max_total_size"] == 9
        assert screen.limits_result is original
        checked.append(True)
        return "esc"

    events = [cancel, after_field] if kind in ("field", "invalid-field") else [cancel]
    tty_prompt(events)
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    assert snug._run_screen(screen) is None
    if kind == "members":
        assert screen.members_result == ["one"] and screen.marked == {"one"}
    elif kind in ("limits", "field", "invalid-field"):
        assert screen.limits_result is original
        assert snug.ExtractionLimits(**screen.values) == original
        assert checked == ([True] if kind != "limits" else [])
    elif kind == "options":
        assert screen.result is None
    else:
        assert screen.lines == ["error: safe failure" if kind == "error" else "completed result"]


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("error", [False, True])
@pytest.mark.parametrize("cancel", CANCELS, ids=["Escape", "Ctrl-C"])
def test_result_and_error_pause_return_to_live_main_menu(
        tty_prompt, monkeypatch, size, error, cancel):
    observed = []

    def handler(engine):
        if error:
            raise snug.ArchiveError("safe failure")
        with snug._menu_result_output():
            print("completed result")

    def main_again(output):
        rendered = "\n".join(_terminal_rows(output.getvalue(), size).values())
        assert "Select an action" in rendered
        observed.append(True)
        return "0"

    tty_prompt(["3", cancel, main_again])
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    monkeypatch.setattr(snug, "_MENU_HANDLERS", {"3": handler})
    assert snug._interactive_menu() == 0
    assert observed == [True]


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("cancel", CANCELS, ids=["Escape", "Ctrl-C"])
def test_member_limits_field_and_option_interrupts_return_through_real_review_workflow(
        tty_prompt, monkeypatch, tmp_path, size, cancel):
    archive = make_zip(tmp_path / "archive.zip")
    monkeypatch.chdir(tmp_path)
    engine = snug.ArchiveEngine(backends=[snug.NativeBackend()])
    inspect = Mock(wraps=engine.inspect)
    extract = Mock(wraps=engine.extract)
    monkeypatch.setattr(engine, "inspect", inspect)
    monkeypatch.setattr(engine, "extract", extract)
    monkeypatch.setattr(snug, "ArchiveEngine", lambda: engine)
    checks = []
    options_seen = []
    real_configure = snug._configure_extract

    def configure(engine, options):
        options_seen.append(options)
        return real_configure(engine, options)

    monkeypatch.setattr(snug, "_configure_extract", configure)

    def review(name, next_key):
        def callback(output):
            rendered = "\n".join(_terminal_rows(output.getvalue(), size).values())
            assert "Extract archive options" in rendered
            assert "Members: all" in rendered
            assert options_seen[0].members is None
            assert options_seen[0].limits == snug.ExtractionLimits()
            checks.append(name)
            if isinstance(next_key, BaseException):
                raise next_key
            return next_key
        return callback

    def limits_overview(output):
        rendered = "\n".join(_terminal_rows(output.getvalue(), size).values())
        assert "Extraction limits" in rendered and "Selected entries: none" in rendered
        checks.append("field")
        if isinstance(cancel, BaseException):
            raise cancel
        return cancel

    def main_again(output):
        rendered = "\n".join(_terminal_rows(output.getvalue(), size).values())
        assert "Select an action" in rendered
        checks.append("main")
        return "0"

    tty_prompt([
        "2", "1", "m", "n", "1", cancel,
        review("members", "l"), "e", "7", "enter", cancel,
        review("limits", "l"), "e", "9", cancel, limits_overview,
        review("field-return", cancel), "enter", main_again,
    ])
    monkeypatch.setattr(snug, "_term_size", lambda: size)

    assert snug._interactive_menu() == 0
    assert checks == ["members", "limits", "field", "field-return", "main"]
    inspect.assert_called_once_with(archive, password=None)
    extract.assert_not_called()


@pytest.mark.parametrize("size", SIZES)
def test_main_menu_ctrl_c_still_quits_with_130(tty_prompt, monkeypatch, size):
    tty_prompt([KeyboardInterrupt()])
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    assert snug._interactive_menu() == 130


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("filtering", ["off", "empty", "active"])
def test_picker_ctrl_c_immediately_cancels_in_both_modes(
        tty_prompt, monkeypatch, tmp_path, single, filtering):
    source = tmp_path / "keep.zip"
    source.touch()
    keys = []
    if not single:
        keys.append("space")
    if filtering != "off":
        keys.append("/")
    if filtering == "active":
        keys.append("k")
    _, reads = tty_prompt([*keys, KeyboardInterrupt()])
    assert snug._select_sources_arrow(tmp_path, single=single) is None
    assert len(reads) == len(keys) + 1


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("filtering", ["empty", "active"])
@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
def test_picker_escape_first_clears_filter_then_cancels_preserving_marks_and_location(
        tty_prompt, monkeypatch, tmp_path, single, filtering, size):
    source = tmp_path / "keep.zip"
    source.touch()
    screen = snug._PickerScreen(tmp_path, single=single)
    if not single:
        screen.handle("space")
    before = (screen.current, deepcopy(screen.marked))
    screen.handle("/")
    if filtering == "active":
        screen.handle("k")
    assert screen.handle("esc") is None
    assert screen.filter == "" and screen.filter_mode is False
    assert (screen.current, screen.marked) == before
    assert screen.result is None
    assert screen.handle("esc") is snug._EXIT
    assert screen.result is None

    # Exercise actual reader/loop routing as well as the pure handler.
    cleared = []

    def after_clear(output):
        rendered = "\n".join(_terminal_rows(output.getvalue(), size).values())
        assert source.name in rendered
        assert "Filter:" not in rendered
        cleared.append(True)
        return "esc"

    keys = ["/"] + (["k"] if filtering == "active" else [])
    tty_prompt([*keys, "esc", after_clear])
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    assert snug._select_sources_arrow(tmp_path, single=single) is None
    assert cleared == [True]


@pytest.mark.parametrize("tty_side", ["neither", "stdin", "stdout"])
@pytest.mark.parametrize("failure", [KeyboardInterrupt(), EOFError()])
@pytest.mark.parametrize("kind", ["menu", "members", "limits", "field", "prompt", "pause", "picker"])
def test_non_tty_paths_keep_their_existing_input_and_exit_contracts(
        monkeypatch, tmp_path, tty_side, failure, kind):
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: tty_side == "stdin"))
    monkeypatch.setattr(snug.sys.stdout, "isatty", lambda: tty_side == "stdout")
    reader = Mock(side_effect=failure)
    monkeypatch.setattr("builtins.input", reader)
    forbidden = Mock(side_effect=AssertionError("non-TTY path used screen loop"))
    monkeypatch.setattr(snug, "_run_screen", forbidden)
    original = snug.ExtractionLimits(max_entries=3)
    if kind == "menu":
        assert snug._select_menu("Options", [("b", "Back")]) is None
    elif kind == "members":
        engine = Mock()
        engine.inspect.return_value = SimpleNamespace(entries=[snug.ArchiveEntry("one")])
        assert snug._menu_members(engine, tmp_path / "archive.zip", None, ["one"]) == ["one"]
    elif kind == "limits":
        assert snug._menu_limits(original) is original
    elif kind in ("field", "prompt"):
        handler = (lambda engine: snug._menu_limit_value(3, snug._parse_count)) if kind == "field" else (
            lambda engine: snug._prompt("Value"))
        assert snug._run_menu_handler(Mock(), handler) == 0
    elif kind == "pause":
        assert snug._wait_for_enter() is None
        reader.assert_not_called()
    else:
        assert snug._select_sources_arrow(tmp_path, single=True) is None
        reader.assert_not_called()
    forbidden.assert_not_called()


@pytest.mark.parametrize("kind", ["menu", "pause", "members", "limits", "field"])
def test_tty_raw_eof_remains_distinct_from_ctrl_c(tty_prompt, monkeypatch, kind):
    keys = (["e", "9"] if kind == "field" else []) + [snug._TerminalEOF()]
    tty_prompt(keys)
    if kind in ("limits", "field"):
        with pytest.raises(snug._QuitInteractive if kind == "field" else snug._TerminalEOF):
            snug._menu_limits(snug.ExtractionLimits(max_entries=3))
    else:
        screen = (snug._MenuScreen("Options", None, [("b", "Back")]) if kind == "menu" else
                  snug._PauseScreen(["result"]) if kind == "pause" else
                  snug._MembersScreen(["one"], ["one"]))
        with pytest.raises(snug._TerminalEOF):
            snug._run_screen(screen)


def test_secure_getpass_and_tty_line_prompt_keep_separate_interrupt_contracts(
        tty_prompt, monkeypatch):
    interrupt = KeyboardInterrupt()
    monkeypatch.setattr(snug.getpass, "getpass", Mock(side_effect=interrupt))
    with pytest.raises(KeyboardInterrupt) as raised:
        snug._read_password(SimpleNamespace(password=True, password_file=None))
    assert raised.value is interrupt
    tty_prompt(["x", KeyboardInterrupt()])
    with pytest.raises(snug._CancelPrompt):
        snug._prompt("Value")


@pytest.mark.parametrize("kind", ["menu", "pause", "members", "limits", "field"])
def test_real_unix_ctrl_c_bytes_use_local_escape_contract(unix_input, monkeypatch, tmp_path, kind):
    prefix = b"e9" if kind == "field" else b""
    suffix = b"\x1b" if kind == "field" else b""
    source = unix_input(prefix + b"\x03" + suffix)
    monkeypatch.setattr(snug.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(snug.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(snug, "_raw_mode", nullcontext)
    monkeypatch.setattr(snug, "_term_size", lambda: (40, 10))
    if kind == "menu":
        assert snug._select_menu("Options", [("b", "Back")]) is None
    elif kind == "pause":
        assert snug._wait_for_enter() is None
    elif kind == "members":
        engine = Mock()
        engine.inspect.return_value = SimpleNamespace(entries=[snug.ArchiveEntry("one")])
        assert snug._menu_members(engine, tmp_path / "archive.zip", None, ["one"]) == ["one"]
    else:
        original = snug.ExtractionLimits(max_entries=3)
        assert snug._menu_limits(original) is original
    assert not source.pending
