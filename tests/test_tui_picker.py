"""Source-picker contracts, recoverable read errors, and Unicode matching."""

import errno
import io
from pathlib import Path
import unicodedata

import pytest

import snug
from test_tui_input import unix_input
from test_tui_layout import _terminal_rows


def _render(screen, monkeypatch, size=(80, 24)):
    output = io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", output)
    snug._draw_lines(screen.draw(*size), footer=screen.footer(), size=size)
    return "\n".join(_terminal_rows(output.getvalue(), size).values())


@pytest.fixture
def failed_directories(monkeypatch):
    """Raise real OSError subclasses at deterministic directory-read points."""
    failures = {}
    reads = []
    original = Path.iterdir

    def iterdir(path):
        reads.append(path)
        if path in failures:
            raise failures[path]
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    return failures, reads


def _read_error(kind, directory):
    if kind == "permission":
        return PermissionError(errno.EACCES, "Permission denied", str(directory))
    return FileNotFoundError(errno.ENOENT, "No such file or directory", str(directory))


@pytest.mark.parametrize("marked", [False, True])
def test_enter_opens_a_directory_instead_of_accepting_it(tmp_path, marked):
    child = tmp_path / "child"
    child.mkdir()
    leaf = child / "leaf.txt"
    leaf.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.cursor = screen.entries.index(child)
    if marked:
        screen.handle("space")

    assert screen.handle("enter") is None

    assert screen.current == child
    assert screen.entries == [leaf]
    assert screen.cursor == 0
    assert screen.result is None
    assert screen.marked == ({child} if marked else set())


@pytest.mark.parametrize("previous_marks", [False, True])
def test_enter_on_file_preserves_existing_acceptance_behavior(tmp_path, previous_marks):
    source = tmp_path / "z selected.txt"
    prior = tmp_path / "a marked.txt"
    source.touch()
    prior.touch()
    screen = snug._PickerScreen(tmp_path)
    if previous_marks:
        screen.marked.add(prior)
    screen.cursor = screen.entries.index(source)

    assert screen.handle("enter") is snug._EXIT

    assert screen.result == ([prior] if previous_marks else [source])
    assert screen.marked == ({prior} if previous_marks else set())
    assert screen.current == tmp_path


@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
@pytest.mark.parametrize("filtering", ["off", "empty", "active"])
def test_space_toggles_cursor_mark_without_moving_or_editing_filter(tmp_path, directory, filtering):
    paths = [tmp_path / name for name in ("keep-a", "keep-b", "other")]
    for path in paths:
        path.mkdir() if directory else path.touch()
    screen = snug._PickerScreen(tmp_path)
    if filtering != "off":
        screen.handle("/")
        if filtering == "active":
            for key in "keep":
                screen.handle(key)
    selected = paths[1]
    screen.cursor = screen._visible().index(selected)
    before = (screen.cursor, screen.filter, screen.filter_mode)

    assert screen.handle("space") is None
    assert screen.marked == {selected}
    assert (screen.cursor, screen.filter, screen.filter_mode) == before

    assert screen.handle("space") is None
    assert screen.marked == set()
    assert (screen.cursor, screen.filter, screen.filter_mode) == before


def test_confirm_accepts_exact_sorted_marks_including_hidden_and_other_directories(tmp_path):
    child = tmp_path / "nested"
    child.mkdir()
    hidden = tmp_path / ".hidden"
    hidden.touch()
    leaf = child / "Tiếng Việt 📦.txt"
    leaf.touch()
    visible = child / "visible.txt"
    visible.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.marked.update([leaf, child, hidden])
    screen.cursor = screen.entries.index(child)
    screen.handle("enter")
    for key in "visible":
        screen.handle(key)
    assert screen._visible() == [visible]

    assert screen.handle("confirm") is snug._EXIT

    assert screen.result == sorted([leaf, child, hidden], key=str)
    assert visible not in screen.result
    assert all(isinstance(path, Path) for path in screen.result)


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("filtering", [False, True])
def test_confirm_without_marks_stays_in_picker_and_shows_instruction(tmp_path, monkeypatch, size, filtering):
    source = tmp_path / "keep.txt"
    source.touch()
    screen = snug._PickerScreen(tmp_path)
    if filtering:
        for key in "keep":
            screen.handle(key)
    before = (screen.current, screen.cursor, screen.filter, screen.filter_mode)
    baseline = _render(screen, monkeypatch, size)

    assert screen.handle("confirm") is None

    rendered = _render(screen, monkeypatch, size)
    assert rendered != baseline
    assert "mark" in rendered.lower()
    assert "Space" in rendered
    assert "keep.txt" in rendered
    assert screen.result is None
    assert screen.marked == set()
    assert (screen.current, screen.cursor, screen.filter, screen.filter_mode) == before


def test_right_still_opens_directory_without_accepting_marks(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    leaf = child / "leaf.txt"
    leaf.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.cursor = screen.entries.index(child)
    screen.handle("space")

    assert screen.handle("right") is None

    assert screen.current == child
    assert screen.entries == [leaf]
    assert screen.result is None
    assert screen.marked == {child}


@pytest.mark.parametrize("key", ["enter", "right"])
def test_directory_removed_after_listing_shows_error_and_can_retry(tmp_path, monkeypatch, key):
    child = tmp_path / "removed"
    child.mkdir()
    marked = tmp_path / "marked.txt"
    marked.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.marked.add(marked)
    screen.cursor = screen.entries.index(child)
    child.rmdir()

    assert screen.handle(key) is None
    assert screen.current == child and screen.entries == []
    rendered = _render(screen, monkeypatch, (40, 10))
    assert "Error:" in rendered and "Retry" in rendered
    assert "empty directory" not in rendered
    assert screen.marked == {marked} and screen.result is None

    child.mkdir()
    leaf = child / "leaf.txt"
    leaf.touch()
    assert screen.handle("enter") is None
    assert screen.entries == [leaf]
    assert "Error:" not in _render(screen, monkeypatch, (40, 10))
    assert screen.marked == {marked} and screen.result is None


@pytest.mark.parametrize("key", ["enter", "right"])
def test_directory_replaced_by_symlink_is_not_entered(tmp_path, key):
    child = tmp_path / "replaced"
    child.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "outside-row.txt").touch()
    screen = snug._PickerScreen(tmp_path)
    screen.cursor = screen.entries.index(child)
    child.rmdir()
    child.symlink_to(target, target_is_directory=True)

    screen.handle(key)

    assert screen.current == tmp_path
    assert screen.entries[screen.cursor] == child
    if key == "enter":
        assert screen.result == [child]
    else:
        assert screen.result is None


@pytest.mark.parametrize("kind,reason", [("permission", "permission denied"),
                                         ("missing", "no such file or directory")])
@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
def test_origin_read_error_is_visible_and_not_presented_as_empty(tmp_path, monkeypatch, failed_directories, kind, reason, size):
    failures, reads = failed_directories
    failures[tmp_path] = _read_error(kind, tmp_path)

    screen = snug._PickerScreen(tmp_path)
    rendered = _render(screen, monkeypatch, size).lower()

    assert reads == [tmp_path]
    assert reason in rendered
    assert "error" in rendered
    assert "retry" in rendered and "back" in rendered
    assert "empty directory" not in rendered
    assert screen.entries == []
    assert screen.result is None


@pytest.mark.parametrize("kind", ["permission", "missing"])
def test_enter_retries_failed_directory_and_keeps_marks(tmp_path, monkeypatch, failed_directories, kind):
    child = tmp_path / "child"
    child.mkdir()
    leaf = child / "leaf.txt"
    leaf.touch()
    marked = tmp_path / "marked.txt"
    marked.touch()
    failures, reads = failed_directories
    failures[child] = _read_error(kind, child)
    screen = snug._PickerScreen(tmp_path)
    screen.marked.add(marked)
    screen.cursor = screen.entries.index(child)

    assert screen.handle("enter") is None
    assert screen.current == child
    assert screen.entries == []
    assert "retry" in _render(screen, monkeypatch).lower()
    assert screen.marked == {marked}

    del failures[child]
    assert screen.handle("enter") is None

    rendered = _render(screen, monkeypatch)
    assert reads.count(child) == 2
    assert screen.entries == [leaf]
    assert screen.current == child
    assert screen.cursor == 0
    assert "leaf.txt" in rendered
    assert "Permission denied" not in rendered
    assert "No such file or directory" not in rendered
    assert screen.marked == {marked}
    assert screen.result is None


@pytest.mark.parametrize("kind", ["permission", "missing"])
def test_left_returns_from_failed_directory_restoring_cursor_and_marks(tmp_path, monkeypatch, failed_directories, kind):
    child = tmp_path / "child"
    child.mkdir()
    marked = tmp_path / "marked.txt"
    marked.touch()
    failures, _ = failed_directories
    failures[child] = _read_error(kind, child)
    screen = snug._PickerScreen(tmp_path)
    screen.marked.add(marked)
    screen.cursor = screen.entries.index(child)
    screen.handle("right")
    assert "retry" in _render(screen, monkeypatch).lower()

    assert screen.handle("left") is None

    assert screen.current == tmp_path
    assert screen.entries[screen.cursor] == child
    assert screen.marked == {marked}
    rendered = _render(screen, monkeypatch)
    assert "child/" in rendered and "marked.txt" in rendered
    assert "Permission denied" not in rendered
    assert "No such file or directory" not in rendered


def test_repeated_failed_retry_and_failed_parent_are_recoverable(tmp_path, monkeypatch, failed_directories):
    child = tmp_path / "child"
    child.mkdir()
    marked = tmp_path / "marked.txt"
    marked.touch()
    failures, reads = failed_directories
    failures[child] = _read_error("permission", child)
    screen = snug._PickerScreen(tmp_path)
    screen.marked.add(marked)
    screen.cursor = screen.entries.index(child)
    screen.handle("enter")

    assert screen.handle("enter") is None
    assert reads.count(child) == 2
    assert "permission denied" in _render(screen, monkeypatch).lower()
    assert screen.marked == {marked}

    failures[tmp_path] = _read_error("missing", tmp_path)
    assert screen.handle("left") is None
    assert screen.current == tmp_path
    assert "no such file or directory" in _render(screen, monkeypatch).lower()
    assert screen.marked == {marked}

    del failures[tmp_path]
    assert screen.handle("enter") is None
    assert set(screen.entries) == {child, marked}
    assert screen.marked == {marked}
    rendered = _render(screen, monkeypatch).lower()
    assert "enter retry" not in rendered
    assert "no such file or directory" not in rendered


@pytest.mark.parametrize("name_form,query_form", [("NFD", "NFC"), ("NFC", "NFD")])
@pytest.mark.parametrize("accept", ["enter", "confirm"])
def test_filter_normalizes_comparison_only_preserving_original_display_and_path(tmp_path, monkeypatch, name_form, query_form, accept):
    name = unicodedata.normalize(name_form, "Việt café 📦.txt")
    source = tmp_path / name
    source.touch()
    (tmp_path / "other.txt").touch()
    screen = snug._PickerScreen(tmp_path)
    query = unicodedata.normalize(query_form, "CAFÉ")
    screen.handle("/")
    for character in query:
        screen.handle(character)

    assert screen.filter == query
    assert screen._visible() == [source]
    assert name in _render(screen, monkeypatch)
    if accept == "confirm":
        screen.handle("space")

    assert screen.handle(accept) is snug._EXIT
    assert screen.result == [source]
    assert screen.result[0].name == name
    assert str(screen.result[0]) == str(source)


def test_filter_does_not_change_existing_directory_first_sort_order(tmp_path):
    directory = tmp_path / "café-z"
    directory.mkdir()
    files = [tmp_path / name for name in ("café-b.txt", "cafe\u0301-a.txt", "OTHER.txt")]
    for source in files:
        source.touch()
    screen = snug._PickerScreen(tmp_path)
    expected = [directory] + sorted(files, key=lambda path: path.name.lower())
    assert screen.entries == expected
    for character in "café":
        screen.handle(character)

    assert screen.entries == expected
    assert screen._visible() == [path for path in expected if path != files[2]]


@pytest.mark.parametrize("size", [(80, 24), (80, 16), (60, 12), (40, 10)])
@pytest.mark.parametrize("filtering", [False, True])
def test_new_picker_help_fits_full_and_compact_layouts(tmp_path, monkeypatch, size, filtering):
    for index in range(30):
        (tmp_path / f"source-{index:02d}.txt").touch()
    screen = snug._PickerScreen(tmp_path)
    if filtering:
        screen.handle("/")
        for character in "source":
            screen.handle(character)
    screen.cursor = 23
    screen.handle("space")

    rendered = _render(screen, monkeypatch, size)

    assert "source-23.txt" in rendered
    assert "Marked (1)" in rendered
    assert "Space" in rendered and "Mark" in rendered
    assert "Tab use marks" in rendered if size == (80, 24) else "Tab OK" in rendered
    assert "Esc" in rendered
    assert len(screen.footer()) == (3 if size == (80, 24) else 1)
    if size == (80, 24):
        assert "Enter" in rendered and "→" in rendered
        assert "open dir" in rendered.lower()
    else:
        assert "Open/OK" in rendered


def test_unix_tab_decodes_as_confirm_and_leaves_following_input(unix_input):
    source = unix_input(b"\t" + "é".encode("utf-8"))

    assert snug._read_key_unix() == "confirm"
    assert snug._read_key_unix() == "é"
    assert not source.pending


def test_escape_before_tab_preserves_tab_confirm_event(unix_input):
    source = unix_input(b"\x1b\t")

    assert snug._read_key_unix() == "esc"
    assert snug._read_key_timeout(0) == "confirm"
    assert not source.pending


def test_printable_letters_remain_filter_input_instead_of_confirming(tmp_path):
    source = tmp_path / "tab-confirm.txt"
    source.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.marked.add(source)

    for key in "tab-confirm":
        assert screen.handle(key) is None

    assert screen.filter == "tab-confirm"
    assert screen._visible() == [source]
    assert screen.marked == {source}
    assert screen.result is None
