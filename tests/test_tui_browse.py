"""Single archive browsing through the shared picker and line editor."""

from contextlib import contextmanager, nullcontext
from copy import deepcopy
import errno
from pathlib import Path
import sys
from types import SimpleNamespace
import unicodedata
from unittest.mock import Mock

import pytest

import snug
from test_tui_editor import tty_prompt
from test_tui_layout import _cells, _terminal_rows
from test_tui_options import make_zip, quiet_tui, script_menus
from test_tui_picker import _render, failed_directories


@pytest.fixture(autouse=True)
def isolated_browse_directory(monkeypatch):
    monkeypatch.setattr(snug, "_ARCHIVE_BROWSE_DIRECTORY", None)


@pytest.mark.parametrize("accept", ["enter", "confirm"])
def test_single_accepts_exact_highlighted_file_and_shows_unknown_and_hidden_files(tmp_path, accept):
    paths = [tmp_path / name for name in (".hidden", "document.unknown", " Việt cafe\u0301 📦 ")]
    for path in paths:
        path.touch()
    screen = snug._PickerScreen(tmp_path, single=True)
    assert set(screen.entries) == set(paths)
    chosen = next(path for path in screen.entries if path.name.startswith(" Việt"))
    screen.cursor = screen.entries.index(chosen)

    assert screen.handle(accept) is snug._EXIT
    assert screen.result == [chosen]
    assert str(screen.result[0]) == str(chosen)
    assert screen.result[0].name == chosen.name
    assert screen.marked == set()


@pytest.mark.parametrize("open_key", ["enter", "right"])
def test_single_opens_directory_and_left_restores_highlight(tmp_path, open_key):
    child = tmp_path / "child"
    child.mkdir()
    leaf = child / "leaf.zip"
    leaf.touch()
    screen = snug._PickerScreen(tmp_path, single=True)

    assert screen.handle(open_key) is None
    assert screen.current == child and screen.entries == [leaf]
    assert screen.result is None
    assert screen.handle("left") is None
    assert screen.current == tmp_path
    assert screen.entries[screen.cursor] == child


def test_single_can_go_above_start_directory_but_multi_stays_at_origin(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    single = snug._PickerScreen(child, single=True)
    multi = snug._PickerScreen(child)

    single.handle("left")
    multi.handle("left")
    assert single.current == tmp_path and single.entries[single.cursor] == child
    assert multi.current == child
    root = snug._PickerScreen(Path(tmp_path.anchor), single=True)
    root.handle("left")
    assert root.current == Path(tmp_path.anchor)


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
def test_tab_on_directory_stays_and_shows_visible_hint(tmp_path, monkeypatch, size):
    child = tmp_path / "child"
    child.mkdir()
    screen = snug._PickerScreen(tmp_path, single=True)
    before = (screen.current, screen.cursor)

    assert screen.handle("confirm") is None
    rendered = _render(screen, monkeypatch, size)
    assert "Choose a file" in rendered
    assert (screen.current, screen.cursor) == before
    assert screen.result is None


@pytest.mark.parametrize("accept", ["enter", "confirm"])
def test_empty_or_filtered_empty_single_picker_does_not_accept(tmp_path, accept):
    screen = snug._PickerScreen(tmp_path, single=True)
    assert screen.handle(accept) is None
    assert screen.result is None and "No file" in screen._notice
    (tmp_path / "leaf").touch()
    screen._load_entries()
    screen.handle("z")
    assert screen.handle(accept) is None
    assert screen.result is None


@pytest.mark.parametrize("size", [(80, 24), (80, 16), (60, 12), (40, 10)])
@pytest.mark.parametrize("filtering", [False, True])
def test_space_is_inert_and_single_footer_fits_without_marks(tmp_path, monkeypatch, size, filtering):
    for index in range(30):
        (tmp_path / f"source-{index:02d}.txt").touch()
    screen = snug._PickerScreen(tmp_path, single=True)
    if filtering:
        for key in "source":
            screen.handle(key)
    screen.cursor = 23
    before = (screen.cursor, screen.filter, screen.filter_mode)
    assert screen.handle("space") is None
    assert (screen.cursor, screen.filter, screen.filter_mode) == before
    assert screen.marked == set()
    rendered = _render(screen, monkeypatch, size)

    assert "source-23.txt" in rendered
    assert "Marked" not in rendered and "Space" not in rendered
    assert "Tab" in rendered and "Esc/Ctrl+C" in rendered
    assert "↵ Open/OK" in rendered or "Enter/→ open dir" in rendered
    assert len(screen.footer()) == (3 if size == (80, 24) else 1)
    assert all(_cells(line) <= size[0] - 1 for line in screen.footer())


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("cancel", ["esc", KeyboardInterrupt()], ids=["Escape", "Ctrl-C"])
@pytest.mark.parametrize("filtering", [False, True])
def test_browser_cancel_returns_to_exact_prefilled_prompt(
        tty_prompt, monkeypatch, tmp_path, quiet_tui, size, cancel, filtering):
    draft = " draft cafe\u0301 "
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    checked = []

    def prefilled(output):
        rendered = "\n".join(_terminal_rows(output.getvalue(), size).values())
        assert "Archive path" in rendered and draft in rendered
        assert "Tab Browse" in rendered and "Esc/Ctrl+C" in rendered
        assert snug._PROMPT_HISTORY == {}
        checked.append(True)
        return "esc"

    keys = [*("space" if char == " " else char for char in draft), "confirm"]
    if filtering:
        keys.extend(["/", "x"])
    tty_prompt([*keys, cancel, prefilled])
    monkeypatch.setattr(snug, "_term_size", lambda: size)

    assert snug._handle_manual_path() is None
    assert checked == [True]


@pytest.mark.parametrize("kind", ["directory", "file", "missing-with-parent", "missing-parent", "empty"])
def test_start_directory_uses_typed_path_context_or_cwd(monkeypatch, tmp_path, kind):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "cafe\u0301"
    directory.mkdir()
    file = directory / "archive.zip"
    file.touch()
    raw, expected = {
        "directory": (str(directory), directory),
        "file": (str(file), directory),
        "missing-with-parent": (str(directory / "missing"), directory),
        "missing-parent": (str(tmp_path / "absent" / "missing"), tmp_path),
        "empty": ("", tmp_path),
    }[kind]
    assert snug._archive_browse_start(raw) == expected


def test_relative_and_literal_spaced_directory_start_is_not_trimmed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    exact = tmp_path / " directory "
    exact.mkdir()
    assert snug._archive_browse_start(" directory ") == exact
    assert snug._archive_browse_start(" directory /missing") == exact
    assert snug._archive_browse_start("local-missing") == tmp_path


@pytest.mark.parametrize("finish", ["enter", "esc", KeyboardInterrupt()])
def test_last_directory_remembered_after_selection_or_cancel_and_typed_context_wins(
        tty_prompt, monkeypatch, tmp_path, finish):
    child = tmp_path / "child"
    child.mkdir()
    leaf = child / "leaf"
    leaf.touch()
    monkeypatch.chdir(tmp_path)
    tty_prompt(["right", finish])

    result = snug._select_sources_arrow(tmp_path, single=True)
    assert result == ([leaf] if finish == "enter" else None)
    assert snug._archive_browse_start("") == child
    assert snug._archive_browse_start(str(tmp_path)) == tmp_path
    assert snug._archive_browse_start(str(tmp_path / "missing-parent" / "leaf")) == tmp_path
    leaf.unlink()
    child.rmdir()
    assert snug._archive_browse_start("") == tmp_path


def test_new_interactive_session_clears_browse_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(snug, "_ARCHIVE_BROWSE_DIRECTORY", tmp_path)
    monkeypatch.setattr(snug, "_fullscreen", nullcontext)
    monkeypatch.setattr(snug, "_init_color", lambda: None)

    def choose(engine):
        assert snug._ARCHIVE_BROWSE_DIRECTORY is None
        return 0

    monkeypatch.setattr(snug, "_run_menu_choice", choose)
    assert snug._interactive_menu() == 0


@pytest.mark.parametrize("invalid", ["removed", "replaced-by-directory"])
def test_browsed_result_uses_normal_manual_validation_and_visible_retry(
        tty_prompt, monkeypatch, tmp_path, quiet_tui, invalid):
    chosen = tmp_path / "chosen.unknown"
    chosen.touch()
    good = tmp_path / "good"
    good.touch()
    monkeypatch.chdir(tmp_path)

    def remove_during_selection(output):
        chosen.unlink()
        if invalid == "replaced-by-directory":
            chosen.mkdir()
        return "enter"

    def retry(output):
        rendered = "\n".join(_terminal_rows(output.getvalue(), (80, 24)).values())
        assert "Not a file." in rendered and chosen.name in rendered
        return "ctrl_u"

    # The picker retains its original listing, then the shared path check rejects it.
    # A replaced directory cannot be confirmed in single mode, so inject the
    # selection race after confirmation, before returning to the prompt caller.
    real_picker = snug._select_sources_arrow

    def select(origin, *, single):
        if invalid == "replaced-by-directory":
            result = real_picker(origin, single=single)
            chosen.unlink()
            chosen.mkdir()
            return result
        return real_picker(origin, single=single)

    monkeypatch.setattr(snug, "_select_sources_arrow", select)
    first_confirm = remove_during_selection if invalid == "removed" else "enter"
    tty_prompt(["confirm", first_confirm, retry, *str(good), "enter"])
    assert snug._handle_manual_path() == good


@pytest.mark.parametrize("handler", [snug._menu_extract, snug._menu_list, snug._menu_info, snug._menu_test])
def test_browsed_archive_reaches_existing_engine_for_each_archive_action(
        tty_prompt, monkeypatch, tmp_path, quiet_tui, handler):
    child = tmp_path / "child"
    child.mkdir()
    archive = make_zip(child / " real cafe\u0301.unknown ")
    monkeypatch.chdir(tmp_path)
    tty_prompt(["confirm", "right", "confirm"])
    script_menus(monkeypatch, ["r"] if handler in (snug._menu_extract, snug._menu_test) else [])
    engine = snug.ArchiveEngine(backends=[snug.NativeBackend()])
    method = {snug._menu_extract: "extract", snug._menu_list: "list_entries",
              snug._menu_info: "info", snug._menu_test: "test"}[handler]
    called = Mock(wraps=getattr(engine, method))
    monkeypatch.setattr(engine, method, called)
    handler(engine)
    assert called.call_count == 1
    assert called.call_args.args[0] == archive
    assert called.call_args.args[0].name == archive.name


def test_unsupported_browsed_file_reaches_real_engine_error_path(
        tty_prompt, monkeypatch, tmp_path, quiet_tui):
    chosen = tmp_path / "document.unknown"
    chosen.write_bytes(b"this is ordinary text")
    monkeypatch.chdir(tmp_path)
    output, _ = tty_prompt(["confirm", "enter"])
    engine = snug.ArchiveEngine(backends=[snug.NativeBackend()])
    called = Mock(wraps=engine.list_entries)
    monkeypatch.setattr(engine, "list_entries", called)

    assert snug._run_menu_handler(engine, snug._menu_list) is None
    called.assert_called_once_with(chosen)
    assert "error:" in "\n".join(snug._TUI_LAST_LINES)
    assert "Not a file" not in output.getvalue()


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("leave", ["enter", "left"], ids=["retry", "back"])
def test_single_directory_error_reuses_retry_and_back(tmp_path, monkeypatch, failed_directories, size, leave):
    child = tmp_path / "denied"
    child.mkdir()
    leaf = child / "leaf"
    leaf.touch()
    failures, reads = failed_directories
    failures[child] = PermissionError(errno.EACCES, "Permission denied", str(child))
    screen = snug._PickerScreen(tmp_path, single=True)
    screen.handle("right")
    rendered = _render(screen, monkeypatch, size)
    assert "Permission denied" in rendered and "Enter Retry" in rendered and "← Back" in rendered
    assert "empty directory" not in rendered
    assert screen.handle("enter") is None
    assert reads.count(child) == 2
    del failures[child]

    assert screen.handle(leave) is None
    assert screen.current == (child if leave == "enter" else tmp_path)
    if leave == "enter":
        assert screen.entries == [leaf]
    else:
        assert screen.entries[screen.cursor] == child
    assert "Permission denied" not in _render(screen, monkeypatch, size)


@pytest.mark.parametrize("name_form,query_form", [("NFD", "NFC"), ("NFC", "NFD")])
@pytest.mark.parametrize("accept", ["enter", "confirm"])
def test_single_filter_reuses_nfc_matching_without_rewriting_path(tmp_path, name_form, query_form, accept):
    name = unicodedata.normalize(name_form, "Việt café 📦.unknown")
    source = tmp_path / name
    source.touch()
    (tmp_path / "other").touch()
    screen = snug._PickerScreen(tmp_path, single=True)
    original = next(path for path in screen.entries if path == source)
    for character in unicodedata.normalize(query_form, "CAFÉ"):
        screen.handle(character)

    assert screen._visible() == [original]
    assert screen.handle(accept) is snug._EXIT
    assert str(screen.result[0]) == str(original)


@pytest.mark.parametrize("kind", ["file", "directory", "broken"])
@pytest.mark.parametrize("key", ["enter", "confirm", "right"])
def test_single_symlinks_keep_listing_paths_and_never_enter_directory_links(tmp_path, kind, key):
    target = tmp_path / "target"
    if kind == "directory":
        target.mkdir()
    elif kind == "file":
        target.touch()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=kind == "directory")
    screen = snug._PickerScreen(tmp_path, single=True)
    screen.cursor = screen.entries.index(link)
    assert "link@" in "\n".join(screen.draw(80, 24))

    result = screen.handle(key)
    assert screen.current == tmp_path
    if key != "right" and kind != "directory":
        assert result is snug._EXIT and screen.result == [link]
    else:
        assert result is None and screen.result is None


def test_file_symlink_browsed_through_normal_validation_keeps_link_path(
        tty_prompt, monkeypatch, tmp_path, quiet_tui):
    target = tmp_path / "z-target"
    target.touch()
    link = tmp_path / "a-link"
    link.symlink_to(target)
    monkeypatch.chdir(tmp_path)
    tty_prompt(["confirm", "enter"])
    assert snug._handle_manual_path() == link


def test_multi_select_marks_hidden_files_and_directories_as_before(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    hidden = tmp_path / ".hidden"
    hidden.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.handle("space")
    screen.handle("down")
    screen.handle("space")
    screen.handle("/")
    screen.handle("x")
    assert screen.handle("esc") is None
    assert screen.handle("confirm") is snug._EXIT
    assert screen.result == sorted([child, hidden], key=str)
    assert "Space" in "\n".join(screen.footer())


def test_optional_browse_action_preserves_pure_editor_text_cursor_history_and_draft(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("pure editor performed IO"))
    monkeypatch.setattr(snug, "_read_key_timeout", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    editor = snug._LineEditor(" café\u0301 ", ("older",), {"confirm": "browse"})
    editor.handle("left")
    before = deepcopy(vars(editor))
    assert editor.handle("confirm") == "browse"
    assert vars(editor) == before
    plain = snug._LineEditor("text")
    assert plain.handle("confirm") is None and plain.buffer == "text"
    forbidden.assert_not_called()


def test_prompt_browse_action_unwinds_terminal_contexts_before_returning_buffer(tty_prompt, monkeypatch):
    tty_prompt(["confirm"])
    exits = []

    @contextmanager
    def context(name):
        try:
            yield
        finally:
            exits.append(name)

    monkeypatch.setattr(snug, "_prompt_raw_mode", lambda: context("raw"))
    monkeypatch.setattr(snug, "_prompt_signals", lambda: context("signals"))
    with pytest.raises(snug._PromptAction) as raised:
        snug._prompt("Archive path", initial_text=" café\u0301 ", extra_keys={"confirm": "browse"})
    assert (raised.value.action, raised.value.text) == ("browse", " café\u0301 ")
    assert exits == ["raw", "signals"]
    assert snug._PROMPT_HISTORY == {}


def test_non_tty_prompt_ignores_optional_actions_and_reads_literal_tab(monkeypatch):
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    read = Mock(return_value="with\ta tab")
    monkeypatch.setattr("builtins.input", read)
    editor = Mock(side_effect=AssertionError("non-TTY input used editor"))
    monkeypatch.setattr(snug, "_edit_line", editor)
    assert snug._prompt("Archive path", preserve_spaces=True,
                        extra_keys={"confirm": "browse"}) == "with\ta tab"
    read.assert_called_once_with("  Archive path: ")
    editor.assert_not_called()


def test_windows_tab_reports_same_confirm_event(monkeypatch):
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(getch=lambda: b"\t"))
    assert snug._read_key_windows() == "confirm"
