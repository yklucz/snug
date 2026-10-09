"""Characterize stable TUI behavior without requiring a terminal.

Known input, layout, directory-Enter, and limits-cancellation defects are
deliberately excluded. Existing option-workflow tests remain in their module.
"""

import errno
from copy import deepcopy
import io
import os
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import snug
from test_security import write_archive
from test_tui_options import make_zip, quiet_tui, script_menus, script_prompts


def test_picker_orders_directories_before_files_and_includes_hidden_entries(tmp_path):
    alpha = tmp_path / "alpha"
    zulu = tmp_path / "Zulu"
    alpha.mkdir()
    zulu.mkdir()
    files = [tmp_path / name for name in ("zeta.txt", ".hidden", "Beta.txt")]
    for path in files:
        path.touch()

    screen = snug._PickerScreen(tmp_path)

    assert screen.entries == [alpha, zulu, files[1], files[2], files[0]]


def test_picker_left_stops_at_origin(source_tree):
    screen = snug._PickerScreen(source_tree)
    screen.cursor = screen.entries.index(source_tree / "hello.txt")
    highlighted = screen.entries[screen.cursor]

    screen.handle("left")

    assert screen.current == source_tree
    assert screen.entries[screen.cursor] == highlighted


def test_picker_going_up_restores_cursor_to_directory_just_left(tmp_path):
    (tmp_path / "alpha").mkdir()
    child = tmp_path / "zulu"
    child.mkdir()
    screen = snug._PickerScreen(tmp_path)
    screen.cursor = screen.entries.index(child)

    screen.handle("right")
    assert screen.current == child
    screen.handle("left")

    assert screen.current == tmp_path
    assert screen.entries[screen.cursor] == child


def test_picker_navigates_three_directory_levels(source_tree):
    levels = [source_tree / "nested"]
    levels.append(levels[-1] / "second")
    levels.append(levels[-1] / "third")
    levels[-1].mkdir(parents=True)
    leaf = levels[-1] / "leaf.txt"
    leaf.touch()
    screen = snug._PickerScreen(source_tree)

    for directory in levels:
        screen.cursor = screen.entries.index(directory)
        screen.handle("right")
        assert screen.current == directory
    assert screen.entries == [leaf]

    for directory in reversed(levels):
        screen.handle("left")
        assert screen.current == directory.parent
        assert screen.entries[screen.cursor] == directory


def _make_symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except NotImplementedError:
        pytest.skip("symlinks are unavailable on this platform")
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP):
            pytest.skip(f"symlink creation unavailable: {exc}")
        raise


def test_picker_shows_directory_symlink_without_entering_it(tmp_path):
    target = tmp_path / "directory"
    target.mkdir()
    (target / "inside.txt").touch()
    link = tmp_path / "directory-link"
    _make_symlink(link, target, directory=True)
    screen = snug._PickerScreen(tmp_path)
    screen.cursor = screen.entries.index(link)

    assert any("directory-link@" in row for row in screen.draw(80, 24))
    screen.handle("right")

    assert screen.current == tmp_path
    assert screen.entries[screen.cursor] == link


def test_picker_can_select_a_broken_symlink(tmp_path):
    link = tmp_path / "broken-link"
    _make_symlink(link, tmp_path / "missing")
    assert link.is_symlink() and not link.exists()
    screen = snug._PickerScreen(tmp_path)
    screen.cursor = screen.entries.index(link)

    assert any("broken-link@" in row for row in screen.draw(80, 24))
    screen.handle("space")
    assert screen.handle("enter") is snug._EXIT

    assert screen.result == [link]


def test_picker_marks_survive_navigation_and_filtering(tmp_path):
    directory = tmp_path / "nested"
    directory.mkdir()
    root_file = tmp_path / "keep root.txt"
    nested_file = directory / "nested.txt"
    root_file.touch()
    nested_file.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.cursor = screen.entries.index(root_file)
    screen.handle("space")
    assert screen.marked == {root_file}

    screen.cursor = screen.entries.index(directory)
    screen.handle("right")
    assert screen.current == directory
    assert screen.marked == {root_file}
    screen.cursor = screen.entries.index(nested_file)
    screen.handle("space")
    screen.handle("left")
    assert screen.current == tmp_path
    assert screen.marked == {root_file, nested_file}

    screen.handle("/")
    for character in "keep":
        screen.handle(character)
    assert screen._visible() == [root_file]
    assert screen.marked == {root_file, nested_file}
    assert screen.handle("enter") is snug._EXIT
    assert set(screen.result) == {root_file, nested_file}
    assert len(screen.result) == 2


def test_picker_escape_clears_filter_then_cancels_without_paths(tmp_path):
    source = tmp_path / "keep.txt"
    source.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.handle("space")
    screen.handle("/")
    screen.handle("k")
    assert screen.filter and screen.filter_mode

    assert screen.handle("esc") is None
    assert screen.filter == ""
    assert screen.filter_mode is False
    assert screen.marked == {source}
    assert screen.handle("esc") is snug._EXIT
    assert screen.result is None


def test_picker_empty_directory_has_message_and_no_enter_selection(source_tree):
    screen = snug._PickerScreen(source_tree / "empty_dir")

    assert any("empty directory" in row for row in screen.draw(80, 24))
    assert screen.handle("enter") is snug._EXIT
    assert screen.result is None


@pytest.mark.parametrize("name", ["Tiếng Việt 📦.txt", "two words.txt"])
def test_picker_preserves_exact_unicode_and_spaced_paths(tmp_path, name):
    source = tmp_path / name
    source.write_bytes(b"payload")
    screen = snug._PickerScreen(tmp_path)
    screen.cursor = screen.entries.index(source)

    assert screen.handle("enter") is snug._EXIT
    assert screen.result == [source]


def test_menu_up_and_down_wrap():
    screen = snug._MenuScreen("Actions", None, [("a", "First"), ("b", "Second"), ("c", "Third")])

    screen.handle("up")
    assert screen.selected == 2
    screen.handle("down")
    assert screen.selected == 0


def test_menu_enter_activates_highlighted_option():
    screen = snug._MenuScreen("Actions", None, [("a", "First"), ("b", "Second")])
    screen.handle("down")

    assert screen.handle("enter") is snug._EXIT
    assert screen.result == "b"


def test_menu_exact_shortcut_activates_immediately():
    screen = snug._MenuScreen("Actions", None, [("a", "First"), ("c", "Other")])
    assert screen.selected == 0

    assert screen.handle("c") is snug._EXIT
    assert screen.result == "c"


@pytest.mark.parametrize("key", ["esc", "q"])
def test_menu_cancel_returns_no_choice(key):
    screen = snug._MenuScreen("Actions", None, [("a", "First"), ("b", "Second")])
    screen.handle("down")

    assert screen.handle(key) is snug._EXIT
    assert screen.result is None


@pytest.mark.parametrize("selected", [0, 20, 39])
def test_menu_scroll_window_keeps_selected_option_visible(selected):
    options = [(str(index), f"Choice {index:02d}") for index in range(40)]
    screen = snug._MenuScreen("Actions", "Many options", options)
    screen.selected = selected

    lines = screen.draw(80, 24)
    visible = {label for _, label in options if any(label in line for line in lines)}

    assert options[selected][1] in visible
    assert len(visible) < len(options)


def _script_unix_input(monkeypatch, payload):
    import select

    fd = 12345
    pending = bytearray(payload)
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(fileno=lambda: fd))

    def read(read_fd, count):
        assert read_fd == fd
        result = bytes(pending[:count])
        del pending[:count]
        return result

    def ready(readers, writers, errors, timeout):
        assert readers == [fd]
        return ([fd] if pending else [], [], [])

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(select, "select", ready)
    return pending


@pytest.mark.skipif(os.name != "posix", reason="Unix terminal key reader")
def test_unix_key_reader_recognizes_bare_escape(monkeypatch):
    with monkeypatch.context() as patch:
        pending = _script_unix_input(patch, b"\x1b")
        assert snug._read_key_unix() == "esc"
        assert not pending


@pytest.mark.skipif(os.name != "posix", reason="Unix terminal key reader")
@pytest.mark.parametrize("prefix", [b"\x1b[", b"\x1bO"], ids=["CSI", "SS3"])
@pytest.mark.parametrize("final,key", [(b"A", "up"), (b"B", "down"), (b"C", "right"), (b"D", "left")])
def test_unix_key_reader_recognizes_arrow_sequences(monkeypatch, prefix, final, key):
    with monkeypatch.context() as patch:
        pending = _script_unix_input(patch, prefix + final)
        assert snug._read_key_unix() == key
        assert not pending


@pytest.mark.skipif(os.name != "posix", reason="Unix terminal key reader")
@pytest.mark.parametrize("payload", [b"\r", b"\n"], ids=["CR", "LF"])
def test_unix_key_reader_recognizes_enter(monkeypatch, payload):
    with monkeypatch.context() as patch:
        pending = _script_unix_input(patch, payload)
        assert snug._read_key_unix() == "enter"
        assert not pending


@pytest.mark.skipif(os.name != "posix", reason="Unix terminal key reader")
@pytest.mark.parametrize("payload", [b"", b"\x03"], ids=["EOF", "Ctrl-C"])
def test_unix_key_reader_interrupts_on_eof_and_ctrl_c(monkeypatch, payload):
    with monkeypatch.context() as patch:
        pending = _script_unix_input(patch, payload)
        with pytest.raises(KeyboardInterrupt):
            snug._read_key_unix()
        assert not pending


@pytest.mark.parametrize("handler", [snug._menu_extract, snug._menu_list, snug._menu_info, snug._menu_test])
@pytest.mark.parametrize("cancel", [None, "b"], ids=["Escape", "Back"])
def test_archive_choice_cancellation_never_calls_engine(monkeypatch, tmp_path, quiet_tui, handler, cancel):
    make_zip(tmp_path / "one.zip")
    monkeypatch.chdir(tmp_path)
    seen = script_menus(monkeypatch, [cancel])
    engine = Mock(spec=snug.ArchiveEngine)

    handler(engine)

    assert [title for title, _, _ in seen] == ["Choose an archive"]
    assert engine.mock_calls == []


@pytest.mark.parametrize("handler", [snug._menu_extract, snug._menu_test])
@pytest.mark.parametrize("has_archive", [False, True], ids=["empty-directory", "archive-present"])
def test_blank_manual_path_cancels_without_engine_calls(monkeypatch, tmp_path, quiet_tui, handler, has_archive):
    if has_archive:
        make_zip(tmp_path / "one.zip")
    monkeypatch.chdir(tmp_path)
    seen = script_menus(monkeypatch, ["m", "b"] if has_archive else [])
    script_prompts(monkeypatch, [""])
    prompt = Mock(wraps=snug._prompt)
    monkeypatch.setattr(snug, "_prompt", prompt)
    engine = Mock(spec=snug.ArchiveEngine)

    handler(engine)

    prompt.assert_called_once_with("Archive path", preserve_spaces=True)
    assert len(seen) == (2 if has_archive else 0)
    assert engine.mock_calls == []


@pytest.mark.skipif(os.name != "posix", reason="Unix termios restoration")
@pytest.mark.parametrize("error", [None, RuntimeError, KeyboardInterrupt], ids=["normal", "exception", "interrupt"])
def test_raw_mode_restores_saved_settings(monkeypatch, error):
    import select
    termios = pytest.importorskip("termios")
    tty = pytest.importorskip("tty")
    fd = 12345
    saved = [1, 2, 3, 4, 5, 6, [b"x", b"y"]]
    original = deepcopy(saved)
    events = []
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(fileno=lambda: fd))
    monkeypatch.setattr(termios, "tcgetattr", lambda actual_fd: saved if actual_fd == fd else pytest.fail("wrong input fd"))
    monkeypatch.setattr(tty, "setraw", lambda actual_fd: events.append(("raw", actual_fd)))
    monkeypatch.setattr(termios, "tcsetattr", lambda actual_fd, when, settings: events.append(("restore", actual_fd, deepcopy(settings))))
    # macOS settles restored canonical input with a readiness poll, without
    # consuming bytes; this fixture's descriptor is deliberately synthetic.
    monkeypatch.setattr(select, "select", lambda readers, writers, errors, timeout: ([], [], []))

    def exercise():
        with snug._raw_mode():
            assert events == [("raw", fd)]
            if error is not None:
                raise error("injected")

    if error is None:
        exercise()
    else:
        with pytest.raises(error, match="injected"):
            exercise()
    assert events[-1] == ("restore", fd, original)


def _terminal_modes(output):
    """Interpret cursor/alternate-buffer modes instead of comparing ANSI bytes."""
    modes = {25: True, 1049: False}
    for number, action in re.findall(r"\x1b\[\?(\d+)([hl])", output):
        modes[int(number)] = action == "h"
    return modes[25], modes[1049]


@pytest.mark.parametrize("error", [None, RuntimeError], ids=["normal", "exception"])
def test_fullscreen_restores_cursor_and_original_buffer(monkeypatch, error):
    output = io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", output)

    def exercise():
        with snug._fullscreen():
            assert _terminal_modes(output.getvalue()) == (False, True)
            if error is not None:
                raise error("injected")

    if error is None:
        exercise()
    else:
        with pytest.raises(error, match="injected"):
            exercise()
    assert _terminal_modes(output.getvalue()) == (True, False)


def test_tui_traversal_archive_reports_safe_error_without_writes(monkeypatch, tmp_path, quiet_tui, capsys):
    archive = tmp_path / "traversal.zip"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"existing content")
    write_archive(archive, [("safe.txt", "file", b"safe"), ("../outside.txt", "file", b"evil")])
    destination = tmp_path / "destination"
    monkeypatch.chdir(tmp_path)
    script_menus(monkeypatch, ["1", "d", "r"])
    script_prompts(monkeypatch, [str(destination)])
    engine = snug.ArchiveEngine(backends=[snug.NativeBackend()])
    extract = Mock(wraps=engine.extract)
    monkeypatch.setattr(engine, "extract", extract)

    assert snug._run_menu_handler(engine, snug._menu_extract) is None

    assert extract.call_count == 1
    assert extract.call_args.args == (archive, str(destination))
    output = capsys.readouterr().out
    assert "error: unsafe archive" in output
    assert "path traversal" in output
    assert outside.read_bytes() == b"existing content"
    assert not destination.exists()
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == {Path("outside.txt"), Path("traversal.zip")}


@pytest.mark.parametrize("select_all", [False, True], ids=["initial-all", "apply-all"])
def test_member_confirmation_uses_none_for_all_members(monkeypatch, tmp_path, quiet_tui, select_all):
    archive = make_zip(tmp_path / "one.zip")
    current = ["tree/one.txt"] if select_all else None
    seen = script_menus(monkeypatch, ["a", "r"] if select_all else ["r"])
    engine = snug.ArchiveEngine(backends=[snug.NativeBackend()])

    assert snug._menu_members(engine, archive, None, current) is None

    labels = dict(seen[-1][1])
    assert labels["1"] == "[x] tree/one.txt"
    assert labels["2"] == "[x] tree/two.txt"


@pytest.mark.parametrize("current", [None, ["tree/one.txt"]], ids=["prior-all", "prior-subset"])
def test_member_escape_discards_edits_and_preserves_prior_selection(monkeypatch, tmp_path, quiet_tui, current):
    archive = make_zip(tmp_path / "one.zip")
    original = None if current is None else list(current)
    seen = script_menus(monkeypatch, ["n", "2", None])
    engine = snug.ArchiveEngine(backends=[snug.NativeBackend()])

    assert snug._menu_members(engine, archive, None, current) == original
    assert current == original
    labels = dict(seen[-1][1])
    assert labels["1"] == "[ ] tree/one.txt"
    assert labels["2"] == "[x] tree/two.txt"
