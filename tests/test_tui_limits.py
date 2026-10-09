"""Limit edits stay tentative until Apply, and bounded pauses accept Escape."""

from contextlib import nullcontext
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import snug
from test_tui_layout import SGR, _terminal_rows
from test_tui_options import make_zip, quiet_tui, script_menus, script_prompts


FIELDS = [
    ("e", "max_entries", "7", 7),
    ("s", "max_total_size", "2KiB", 2048),
    ("f", "max_file_size", "1KB", 1000),
    ("r", "max_ratio", "9.5", 9.5),
]


def _type(screen, text):
    for char in text:
        assert screen.handle("space" if char == " " else char) is None


def _edit(screen, key, text):
    assert screen.handle(key) is None
    assert screen.editing == key
    _type(screen, text)
    assert screen.handle("enter") is None
    assert screen.editing is None


@pytest.mark.parametrize("cancel", ["b", "esc", "q", "Q", "quit"])
def test_escape_and_leave_keys_discard_every_tentative_limit(cancel):
    original = snug.ExtractionLimits(3, 4000, 2000, 8)
    screen = snug._LimitsScreen(original)
    for key, field, text, value in FIELDS:
        _edit(screen, key, text)
        assert screen.values[field] == value
        assert screen.limits_result is original

    assert screen.handle(cancel) is snug._EXIT
    assert screen.limits_result is original
    assert snug.ExtractionLimits(**screen.values) == original
    assert original == snug.ExtractionLimits(3, 4000, 2000, 8)


@pytest.mark.parametrize("apply", ["a", "confirm", "enter"])
def test_only_explicit_apply_keeps_all_edited_values(apply):
    original = snug.ExtractionLimits(3, 4000, 2000, 8)
    screen = snug._LimitsScreen(original)
    for key, _, text, _ in FIELDS:
        _edit(screen, key, text)
    screen.selected = 4

    assert screen.limits_result is original
    assert screen.handle(apply) is snug._EXIT
    assert screen.limits_result == snug.ExtractionLimits(7, 2048, 1000, 9.5)
    assert original == snug.ExtractionLimits(3, 4000, 2000, 8)


@pytest.mark.parametrize("index, field", list(enumerate(FIELDS)))
def test_enter_on_field_opens_it_and_blank_enter_keeps_current_value(index, field):
    key, name, _, _ = field
    original = snug.ExtractionLimits(3, 4000, 2000, 8)
    screen = snug._LimitsScreen(original)
    screen.selected = index

    assert screen.handle("enter") is None
    assert screen.editing == key
    assert screen.buffer == ""
    visible = SGR.sub("", "\n".join(screen.draw(80, 24)))
    assert f"Current: {getattr(original, name)}" in visible
    assert screen.handle("enter") is None
    assert screen.editing is None
    assert snug.ExtractionLimits(**screen.values) == original
    assert screen.limits_result is original


@pytest.mark.parametrize("field", FIELDS, ids=lambda field: field[1])
def test_first_field_escape_discards_buffer_next_escape_discards_screen(field):
    key, name, text, _ = field
    original = snug.ExtractionLimits(3, 4000, 2000, 8)
    screen = snug._LimitsScreen(original)
    # The field Escape must preserve another edit that is still tentative.
    other = "s" if key == "e" else "e"
    _edit(screen, other, "11")
    before_field = dict(screen.values)
    assert screen.handle(key) is None
    _type(screen, text)

    assert screen.handle("esc") is None
    assert screen.editing is None
    assert screen.buffer == ""
    assert screen.values == before_field
    assert screen.values[name] == getattr(original, name)
    assert screen.limits_result is original
    assert screen.handle("esc") is snug._EXIT
    assert snug.ExtractionLimits(**screen.values) == original
    assert screen.limits_result is original


@pytest.mark.parametrize("key, text, error", [
    ("e", "-1", "nonnegative integer"),
    ("e", "1.5", "nonnegative integer"),
    ("s", "-1", "size must"),
    ("s", "0.5B", "whole number"),
    ("f", "1kb", "size must"),
    ("r", "nan", "finite positive"),
    ("r", "inf", "finite positive"),
    ("r", "0", "finite positive"),
])
def test_invalid_field_value_is_visible_and_cannot_be_applied(key, text, error):
    original = snug.ExtractionLimits(3, 4000, 2000, 8)
    screen = snug._LimitsScreen(original)
    before = dict(screen.values)
    screen.handle(key)
    _type(screen, text)

    assert screen.handle("enter") is None
    assert screen.editing == key
    assert screen.values == before
    visible = SGR.sub("", "\n".join(screen.draw(80, 24)))
    assert "error:" in visible and error in visible
    assert screen.handle("confirm") is None
    assert screen.editing == key
    assert screen.values == before
    assert screen.limits_result is original
    assert screen.handle("esc") is None
    assert screen.handle("b") is snug._EXIT
    assert screen.limits_result == original


def test_invalid_field_can_be_corrected_without_applying_until_overview_confirm():
    original = snug.ExtractionLimits(max_entries=3)
    screen = snug._LimitsScreen(original)
    screen.handle("e")
    _type(screen, "-1")
    screen.handle("enter")
    assert screen._notice
    screen.handle("backspace")
    screen.handle("backspace")
    _type(screen, "5")

    assert screen.handle("enter") is None
    assert screen.editing is None and screen._notice is None
    assert screen.values["max_entries"] == 5
    assert screen.limits_result is original
    assert screen.handle("confirm") is snug._EXIT
    assert screen.limits_result == snug.ExtractionLimits(max_entries=5)


def test_tab_during_a_valid_unfinished_field_does_not_apply_or_save_it():
    original = snug.ExtractionLimits(max_entries=3)
    screen = snug._LimitsScreen(original)
    screen.handle("e")
    _type(screen, "12")

    assert screen.handle("confirm") is None
    assert screen.editing == "e" and screen.buffer == "12"
    assert screen.values["max_entries"] == 3
    assert screen.limits_result is original
    assert screen.handle("esc") is None
    assert screen.handle("esc") is snug._EXIT
    assert screen.limits_result is original


def test_field_backspace_and_space_preserve_existing_stripped_input_behavior():
    screen = snug._LimitsScreen(snug.ExtractionLimits(max_entries=3))
    screen.handle("e")
    _type(screen, " 123")
    screen.handle("backspace")
    screen.handle("space")
    assert screen.buffer == " 12 "
    assert screen.handle("enter") is None
    assert screen.values["max_entries"] == 12
    assert screen.handle("confirm") is snug._EXIT
    assert screen.limits_result == snug.ExtractionLimits(max_entries=12)


def test_tty_limits_can_still_clear_all_four_fields_with_none():
    screen = snug._LimitsScreen(snug.ExtractionLimits(3, 4000, 2000, 8))
    for key, _, _, _ in FIELDS:
        _edit(screen, key, "none")
    assert screen.handle("confirm") is snug._EXIT
    assert screen.limits_result == snug.ExtractionLimits()


def test_plain_menu_cancel_discards_edits_instead_of_acting_as_apply(
        monkeypatch, quiet_tui):
    original = snug.ExtractionLimits(3, 4000, 2000, 8)
    seen = script_menus(monkeypatch, ["e", "s", "f", "r", None])
    script_prompts(monkeypatch, [field[2] for field in FIELDS])

    assert snug._menu_limits(original) is original
    labels = dict(seen[-1][1])
    assert labels["e"].endswith("7") and labels["s"].endswith("2048")
    assert labels["f"].endswith("1000") and labels["r"].endswith("9.5")
    assert original == snug.ExtractionLimits(3, 4000, 2000, 8)


def _tty(monkeypatch):
    class Output(io.StringIO):
        def isatty(self):
            return True

    output = Output()
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(snug.sys, "stdout", output)
    monkeypatch.setattr(snug, "_raw_mode", nullcontext)
    monkeypatch.setattr(snug, "_term_size", lambda: (80, 24))
    monkeypatch.setattr(snug, "_TUI_LAST_LINES", [])
    return output


@pytest.mark.parametrize("cancel", [False, True], ids=["apply", "discard"])
def test_edited_limits_reach_engine_only_after_apply(
        monkeypatch, tmp_path, quiet_tui, cancel):
    archive = make_zip(tmp_path / "one.zip")
    destination = tmp_path / "output"
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, ["d", "l", "r"])
    script_prompts(monkeypatch, [str(destination)])
    _tty(monkeypatch)
    engine = snug.ArchiveEngine(backends=[snug.NativeBackend()])
    extract = Mock(wraps=engine.extract)
    monkeypatch.setattr(engine, "extract", extract)

    def edit(screen):
        assert isinstance(screen, snug._LimitsScreen)
        _edit(screen, "e", "1")
        assert extract.call_count == 0
        assert screen.handle("esc" if cancel else "confirm") is snug._EXIT
        assert extract.call_count == 0

    monkeypatch.setattr(snug, "_run_screen", edit)
    if cancel:
        snug._menu_extract(engine)
        assert (destination / "tree/one.txt").read_bytes() == b"one"
        assert (destination / "tree/two.txt").read_bytes() == b"second"
    else:
        with pytest.raises(snug.ResourceLimitError):
            snug._menu_extract(engine)
    assert extract.call_count == 1
    assert extract.call_args.kwargs["limits"] == snug.ExtractionLimits(
        max_entries=None if cancel else 1)


def test_reopening_limits_rollback_preserves_prior_apply_and_next_operation_defaults(
        monkeypatch, tmp_path, quiet_tui):
    archive = make_zip(tmp_path / "one.zip")
    destinations = [tmp_path / "first", tmp_path / "next"]
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, ["d", "l", "l", "r", "d", "r"])
    script_prompts(monkeypatch, [str(path) for path in destinations])
    _tty(monkeypatch)
    engine = snug.ArchiveEngine(backends=[snug.NativeBackend()])
    extract = Mock(wraps=engine.extract)
    monkeypatch.setattr(engine, "extract", extract)
    screens = []

    def edit(screen):
        screens.append(screen)
        if len(screens) == 1:
            assert screen.limits_result == snug.ExtractionLimits()
            _edit(screen, "e", "2")
            assert screen.handle("confirm") is snug._EXIT
        else:
            assert screen.limits_result == snug.ExtractionLimits(max_entries=2)
            _edit(screen, "e", "0")
            assert screen.handle("esc") is snug._EXIT
            assert screen.limits_result == snug.ExtractionLimits(max_entries=2)
        assert extract.call_count == 0

    monkeypatch.setattr(snug, "_run_screen", edit)
    snug._menu_extract(engine)
    snug._menu_extract(engine)

    assert len(screens) == 2
    assert [call.kwargs["limits"] for call in extract.call_args_list] == [
        snug.ExtractionLimits(max_entries=2), snug.ExtractionLimits()]
    for destination in destinations:
        assert (destination / "tree/one.txt").read_bytes() == b"one"
        assert (destination / "tree/two.txt").read_bytes() == b"second"


@pytest.mark.parametrize("editing", [False, True], ids=["overview", "field"])
def test_limits_ctrl_c_discards_overview_or_only_active_field(
        monkeypatch, editing):
    _tty(monkeypatch)
    original = snug.ExtractionLimits(max_entries=3)
    seen = []

    real_loop = snug._run_screen

    def observe(screen):
        seen.append(screen)
        real_loop(screen)

    def overview(timeout):
        assert seen[0].editing is None and seen[0].buffer == ""
        assert seen[0].values["max_entries"] == 3
        return "esc"

    events = iter(["e", "9", KeyboardInterrupt(), overview] if editing else [KeyboardInterrupt()])

    def read(timeout):
        event = next(events)
        if isinstance(event, BaseException):
            raise event
        return event(timeout) if callable(event) else event

    monkeypatch.setattr(snug, "_run_screen", observe)
    monkeypatch.setattr(snug, "_read_key_timeout", read)
    assert snug._menu_limits(original) is original
    assert seen[0].limits_result is original
    assert original == snug.ExtractionLimits(max_entries=3)


@pytest.mark.parametrize("key", ["esc", "enter"])
def test_pause_accepts_escape_and_enter(key):
    screen = snug._PauseScreen(["result"])
    assert screen.handle(key) is snug._EXIT


@pytest.mark.parametrize("key", ["q", "space", "confirm", "down"])
def test_pause_still_ignores_unrelated_keys(key):
    screen = snug._PauseScreen(["result"])
    assert screen.handle(key) is None
    assert screen.lines == ["result"]


@pytest.mark.parametrize("error", [False, True], ids=["result", "error"])
@pytest.mark.parametrize("key", ["esc", "enter"])
def test_result_and_error_workflows_dismiss_with_escape_or_enter(monkeypatch, error, key):
    output = _tty(monkeypatch)
    events = iter([None, key])
    seen = []

    def read(timeout):
        event = next(events)
        seen.append(event)
        return event

    def handler(engine):
        if error:
            raise snug.ArchiveError("safe failure")
        with snug._menu_result_output():
            print("completed extraction result")

    monkeypatch.setattr(snug, "_read_key_timeout", read)
    assert snug._run_menu_handler(SimpleNamespace(), handler) is None
    assert seen == [None, key]
    visible = SGR.sub("", "\n".join(_terminal_rows(output.getvalue(), (80, 24)).values()))
    assert ("error: safe failure" if error else "completed extraction result") in visible
    assert "Enter" in visible and "Esc" in visible


def test_ctrl_c_on_result_pause_dismisses_without_exit_code(monkeypatch):
    output = _tty(monkeypatch)

    def handler(engine):
        with snug._menu_result_output():
            print("completed result before interrupt")

    def interrupt(timeout):
        raise KeyboardInterrupt

    monkeypatch.setattr(snug, "_read_key_timeout", interrupt)
    assert snug._run_menu_handler(SimpleNamespace(), handler) is None
    visible = SGR.sub("", "\n".join(_terminal_rows(output.getvalue(), (80, 24)).values()))
    assert "completed result before interrupt" in visible


@pytest.mark.parametrize("size", [(40, 10), (60, 12), (80, 16), (80, 24)])
@pytest.mark.parametrize("state", ["overview", "field", "invalid"])
def test_limits_fields_errors_and_discard_footer_fit_layout(monkeypatch, size, state):
    screen = snug._LimitsScreen(snug.ExtractionLimits(3, 4000, 2000, 8))
    if state != "overview":
        screen.handle("e")
        if state == "invalid":
            _type(screen, "-1")
            screen.handle("enter")
    output = io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", output)

    body = screen.draw(*size)
    footer = screen.footer()
    assert len(body) + len(footer) <= size[1]
    snug._draw_lines(body, footer=footer, size=size)
    visible = SGR.sub("", "\n".join(_terminal_rows(output.getvalue(), size).values()))
    assert "Esc" in visible and "discard" in visible.lower()
    if state == "overview":
        assert "Tab" in visible and "Apply" in visible
        assert "Apply limits" in visible and "Selected entries: 3" in visible
        assert "Back (discard edits)" in visible
    else:
        assert "Enter" in visible and "Current: 3" in visible
        assert "field" in visible
        if state == "invalid":
            assert "error:" in visible and "nonnegative" in visible
    assert len(footer) == (2 if size == (80, 24) else 1)
