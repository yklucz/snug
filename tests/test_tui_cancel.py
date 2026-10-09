"""Prompt cancellation stays local; TTY limits Back rolls edits back."""

import argparse
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import snug
from test_tui_editor import tty_prompt
from test_tui_input import unix_input
from test_tui_layout import _cells, _terminal_rows
from test_tui_limits import FIELDS, _edit
from test_tui_options import make_zip, quiet_tui, script_menus


@pytest.mark.parametrize("cancel", ["esc", KeyboardInterrupt()], ids=["Escape", "Ctrl-C"])
@pytest.mark.parametrize("kind", ["manual", "password-file", "compression", "output",
                                  "overwrite", "destination", "strip"])
def test_ctrl_c_and_escape_share_each_prompt_cancellation_contract(
        tty_prompt, monkeypatch, tmp_path, quiet_tui, cancel, kind):
    tty_prompt(["x", cancel])
    monkeypatch.chdir(tmp_path)
    prior = tmp_path / "prior.zip"
    prior.write_bytes(b"unchanged")
    if kind == "manual":
        assert snug._handle_manual_path() is None
    elif kind == "password-file":
        script_menus(monkeypatch, ["f"])
        password = Mock(side_effect=AssertionError("cancelled password file was opened"))
        monkeypatch.setattr(snug, "_read_password", password)
        assert snug._menu_password("secret") == "secret"
        password.assert_not_called()
    elif kind == "compression":
        assert snug._menu_compression(4) == 4
    elif kind == "output":
        options = snug._CreateOptions(tmp_path, [], [snug.ArchiveFormat.ZIP],
                                      snug.ArchiveFormat.ZIP, prior)
        options.select_output()
        assert options.archive_path == prior and options.fmt is snug.ArchiveFormat.ZIP
    elif kind == "overwrite":
        assert snug._menu_confirm_overwrite(prior) is False
        assert prior.read_bytes() == b"unchanged"
    else:
        options = snug._ExtractOptions(prior, False, destination="previous", strip_components=2)
        if kind == "destination":
            options.select_destination()
            assert options.destination == "previous"
        else:
            options.select_strip_components()
            assert options.strip_components == 2
    assert snug._PROMPT_HISTORY == {}


@pytest.mark.parametrize("has_archive", [False, True])
def test_archive_prompt_ctrl_c_returns_to_menu_without_engine_call_or_session_quit(
        tty_prompt, monkeypatch, tmp_path, quiet_tui, has_archive):
    tty_prompt(["x", KeyboardInterrupt()])
    monkeypatch.chdir(tmp_path)
    if has_archive:
        make_zip(tmp_path / "archive.zip")
    script_menus(monkeypatch, ["m", "b"] if has_archive else [])
    engine = Mock(spec=snug.ArchiveEngine)

    assert snug._run_menu_handler(engine, snug._menu_extract) is None
    assert engine.mock_calls == []


def test_overwrite_ctrl_c_never_calls_creation_engine(
        tty_prompt, monkeypatch, tmp_path, quiet_tui):
    tty_prompt(["y", KeyboardInterrupt()])
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"original archive")
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda origin: [source])
    script_menus(monkeypatch, ["r"])
    engine = Mock(spec=snug.ArchiveEngine)
    engine.writable_formats.return_value = [snug.ArchiveFormat.TAR_GZ]

    assert snug._run_menu_handler(engine, snug._menu_create) is None
    engine.create.assert_not_called()
    assert archive.read_bytes() == b"original archive"


def test_empty_ctrl_d_keeps_session_quit_status_zero(tty_prompt, quiet_tui):
    tty_prompt(["ctrl_d"])
    engine = Mock(spec=snug.ArchiveEngine)

    assert snug._run_menu_handler(engine, lambda engine: snug._prompt("Value")) == 0
    assert engine.mock_calls == []


def test_nonempty_ctrl_d_deletes_forward_and_keeps_prompt_active(tty_prompt):
    tty_prompt(["home", "ctrl_d", "enter"])

    assert snug._prompt("Value", initial_text="e\u0301界") == "界"
    assert snug._PROMPT_HISTORY == {"Value": ["界"]}


def test_raw_descriptor_eof_still_quits_instead_of_cancelling_prompt(unix_input, monkeypatch):
    unix_input(b"", eof=True)
    monkeypatch.setattr(snug.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(snug.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(snug, "_term_size", lambda: (40, 10))
    monkeypatch.setattr(snug, "_prompt_raw_mode", nullcontext)
    monkeypatch.setattr(snug, "_prompt_signals", nullcontext)

    with pytest.raises(snug._QuitInteractive) as raised:
        snug._prompt("Value")
    assert isinstance(raised.value.__cause__, snug._TerminalEOF)


@pytest.mark.parametrize("payload", [b"\x03", b""], ids=["Ctrl-C", "EOF"])
@pytest.mark.parametrize("screen", ["main", "picker", "members", "limits", "limits-field"])
def test_screens_distinguish_local_ctrl_c_cancellation_from_raw_eof(
        unix_input, monkeypatch, tmp_path, payload, screen):
    prefix = b"e9" if screen == "limits-field" else b""
    suffix = b"\x1b" if screen == "limits-field" and payload else b""
    unix_input(prefix + payload + suffix, eof=not payload)
    monkeypatch.setattr(snug.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(snug.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(snug, "_term_size", lambda: (40, 10))
    monkeypatch.setattr(snug, "_raw_mode", nullcontext)
    if screen == "main":
        assert snug._run_menu_choice(Mock()) == 130
    elif screen == "picker":
        assert snug._select_sources_arrow(tmp_path) is None
    elif screen == "members":
        engine = Mock(spec=snug.ArchiveEngine)
        engine.inspect.return_value = SimpleNamespace(entries=[snug.ArchiveEntry("one")])
        if payload:
            assert snug._menu_members(engine, tmp_path / "archive.zip", None, ["one"]) == ["one"]
        else:
            with pytest.raises(KeyboardInterrupt):
                snug._menu_members(engine, tmp_path / "archive.zip", None, ["one"])
        engine.inspect.assert_called_once()
        engine.extract.assert_not_called()
    else:
        original = snug.ExtractionLimits(max_entries=3)
        if payload:
            assert snug._menu_limits(original) is original
        else:
            with pytest.raises(snug._QuitInteractive if screen == "limits-field" else KeyboardInterrupt):
                snug._menu_limits(original)
        assert original == snug.ExtractionLimits(max_entries=3)


@pytest.mark.parametrize("failure", [EOFError(), KeyboardInterrupt()])
@pytest.mark.parametrize("tty_side", ["neither", "stdin", "stdout"])
def test_non_tty_input_keeps_existing_eof_and_interrupt_session_exit(
        monkeypatch, quiet_tui, failure, tty_side):
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: tty_side == "stdin"))
    monkeypatch.setattr(snug.sys.stdout, "isatty", lambda: tty_side == "stdout")
    read = Mock(side_effect=failure)
    monkeypatch.setattr("builtins.input", read)

    assert snug._run_menu_handler(Mock(), lambda engine: snug._prompt("Value")) == 0
    read.assert_called_once_with("  Value: ")


def test_secure_getpass_ctrl_c_is_still_propagated(monkeypatch):
    interrupt = KeyboardInterrupt()
    secure = Mock(side_effect=interrupt)
    monkeypatch.setattr(snug.getpass, "getpass", secure)
    editor = Mock(side_effect=AssertionError("password entered line editor"))
    monkeypatch.setattr(snug, "_prompt", editor)

    with pytest.raises(KeyboardInterrupt) as raised:
        snug._read_password(argparse.Namespace(password=True, password_file=None))
    assert raised.value is interrupt
    secure.assert_called_once_with("Password: ")
    editor.assert_not_called()


@pytest.mark.parametrize("cancel", ["b", "esc", "q", "Q"])
def test_limits_b_and_other_cancel_keys_restore_all_opening_values(cancel):
    original = snug.ExtractionLimits(3, 4000, 2000, 8)
    screen = snug._LimitsScreen(original)
    for key, field, text, value in FIELDS:
        _edit(screen, key, text)
        assert screen.values[field] == value

    assert screen.handle(cancel) is snug._EXIT
    assert screen.limits_result is original
    assert snug.ExtractionLimits(**screen.values) == original


@pytest.mark.parametrize("key", ["b", "q", "Q"])
def test_limits_cancel_shortcuts_remain_literal_text_during_field_edit(key):
    original = snug.ExtractionLimits(max_entries=3)
    screen = snug._LimitsScreen(original)
    screen.handle("e")

    assert screen.handle(key) is None
    assert screen.editing == "e" and screen.buffer == key
    assert screen.limits_result is original
    assert screen.handle("esc") is None
    assert screen.handle("b") is snug._EXIT
    assert screen.limits_result is original


@pytest.mark.parametrize("apply", ["a", "confirm", "enter"])
def test_limits_explicit_apply_still_keeps_edited_values(apply):
    original = snug.ExtractionLimits(max_entries=3)
    screen = snug._LimitsScreen(original)
    _edit(screen, "e", "7")
    screen.selected = next(index for index, (key, _) in enumerate(screen.options) if key == "a")

    assert screen.handle(apply) is snug._EXIT
    assert screen.limits_result == snug.ExtractionLimits(max_entries=7)


def test_enter_on_limits_back_discards():
    original = snug.ExtractionLimits(max_entries=3)
    screen = snug._LimitsScreen(original)
    _edit(screen, "e", "7")
    screen.selected = next(index for index, (key, _) in enumerate(screen.options) if key == "b")

    assert screen.handle("enter") is snug._EXIT
    assert screen.limits_result is original


@pytest.mark.parametrize("finish", ["b", "confirm"])
def test_limits_b_edits_never_reach_extraction_but_tab_applied_edits_do(
        tty_prompt, monkeypatch, tmp_path, quiet_tui, finish):
    archive = make_zip(tmp_path / "archive.zip")
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, ["l", "r"])
    tty_prompt(["e", "1", "enter", finish])
    # Drive the limits overview and field through the real shared screen loop.
    engine = Mock(spec=snug.ArchiveEngine)
    engine.extract.return_value = snug.ExtractReport(archive, tmp_path)

    snug._menu_extract(engine)

    engine.extract.assert_called_once()
    assert engine.extract.call_args.kwargs["limits"] == snug.ExtractionLimits(
        max_entries=None if finish == "b" else 1)


@pytest.mark.parametrize("size", [(40, 10), (80, 24)])
@pytest.mark.parametrize("kind", ["prompt", "limits"])
def test_cancel_hints_and_limits_back_fit_full_and_compact_budgets(
        tty_prompt, monkeypatch, size, kind):
    output, _ = tty_prompt([KeyboardInterrupt()])
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    if kind == "prompt":
        with pytest.raises(snug._CancelPrompt):
            snug._prompt("Value")
    else:
        screen = snug._LimitsScreen(snug.ExtractionLimits())
        snug._draw_lines(screen.draw(*size), screen.footer(), size=size)
    rows = _terminal_rows(output.getvalue(), size)
    visible = "\n".join(rows.values())
    assert len(rows) <= size[1]
    assert all(_cells(line) <= size[0] - 1 for line in rows.values())
    if kind == "prompt":
        assert "Esc/Ctrl+C: cancel" in visible
    else:
        assert "Tab" in visible and "Apply" in visible and "Back (discard edits)" in visible
        assert "b" in visible and "Esc" in visible and "q" in visible and "Q" in visible
