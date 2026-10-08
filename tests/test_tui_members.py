"""Persistent member edits and exact CLI/TUI selection, without a TTY."""

import io
from pathlib import Path
import re
import tarfile
from unittest.mock import Mock
import zipfile

import pytest

import snug
from test_security import write_archive
from test_tui_layout import SGR, _terminal_rows
from test_tui_options import make_zip, quiet_tui, script_menus, script_prompts


@pytest.mark.parametrize("index", [0, 1, 3, 5])
def test_space_toggles_exact_member_and_keeps_cursor(index):
    names = [f"member-{number}.txt" for number in range(6)]
    screen = snug._MembersScreen(names, [])
    screen.selected = index + 4

    assert screen.handle("space") is None
    assert screen.selected == index + 4
    assert screen.marked == {names[index]}
    assert dict(screen.options)[str(index + 1)] == f"[x] {names[index]}"

    assert screen.handle("space") is None
    assert screen.selected == index + 4
    assert screen.marked == set()
    assert dict(screen.options)[str(index + 1)] == f"[ ] {names[index]}"


def test_several_toggles_keep_first_middle_and_last_rows_selected():
    names = [f"member-{number}.txt" for number in range(9)]
    screen = snug._MembersScreen(names, [])
    expected = set()
    for index in (0, 4, 8, 4, 0, 8, 8):
        screen.selected = index + 4
        assert screen.handle("space") is None
        expected.symmetric_difference_update({names[index]})
        assert screen.selected == index + 4
        assert screen.marked == expected
    assert screen.marked == {names[-1]}


@pytest.mark.parametrize("action_row", [0, 1, 2, 3])
def test_space_on_an_action_does_not_activate_it(action_row):
    screen = snug._MembersScreen(["first", "second"], ["first"])
    screen.selected = action_row
    assert screen.handle("space") is None
    assert screen.selected == action_row
    assert screen.marked == {"first"}
    assert screen.members_result == ["first"]


@pytest.mark.parametrize("index", [0, 2])
def test_enter_retains_member_toggle_behavior_and_cursor(index):
    names = ["first", "middle", "last"]
    screen = snug._MembersScreen(names, [])
    screen.selected = index + 4
    assert screen.handle("enter") is None
    assert screen.marked == {names[index]}
    assert screen.selected == index + 4
    assert screen.handle("enter") is None
    assert screen.marked == set()
    assert screen.selected == index + 4


@pytest.mark.parametrize("action_row, expected", [(0, {"first", "second"}), (1, set())])
def test_enter_activates_existing_all_and_none_actions(action_row, expected):
    screen = snug._MembersScreen(["first", "second"], ["first"])
    screen.selected = action_row
    assert screen.handle("enter") is None
    assert screen.selected == action_row
    assert screen.marked == expected


def test_numeric_shortcut_toggles_its_exact_member_without_moving_cursor():
    screen = snug._MembersScreen(["a.txt", "a.txt.more", "a.txt/child"], [])
    screen.selected = 6
    assert screen.handle("2") is None
    assert screen.selected == 6
    assert screen.marked == {"a.txt.more"}


def test_tab_confirms_only_marks_in_archive_order():
    names = ["first", "middle", "last"]
    screen = snug._MembersScreen(names, [])
    for index in (2, 0):
        screen.selected = index + 4
        screen.handle("space")
    assert screen.handle("confirm") is snug._EXIT
    assert screen.members_result == ["first", "last"]


@pytest.mark.parametrize("key", ["confirm", "r", "enter"])
def test_all_marks_keep_existing_none_means_all_contract(key):
    screen = snug._MembersScreen(["first", "second"], None)
    screen.selected = 2
    assert screen.handle(key) is snug._EXIT
    assert screen.members_result is None


@pytest.mark.parametrize("key", ["confirm", "r", "enter"])
@pytest.mark.parametrize("names", [["first", "second"], []], ids=["cleared", "empty-archive"])
def test_zero_mark_confirmation_stays_open_and_shows_instruction(names, key):
    screen = snug._MembersScreen(names, None)
    screen.handle("n")
    screen.selected = 2
    assert screen.handle(key) is None
    assert screen.marked == set()
    assert screen.members_result is None
    assert screen._notice
    output = SGR.sub("", "\n".join(screen.draw(80, 24)))
    assert screen._notice in output
    assert re.search(r"mark|select", screen._notice, re.IGNORECASE)

    if names:
        screen.selected = 4
        screen.handle("space")
        assert screen.handle("confirm") is snug._EXIT
        assert screen.members_result == [names[0]]


@pytest.mark.parametrize("cancel", ["esc", "b", "q", "Q"])
@pytest.mark.parametrize("prior", [None, ["first"], []], ids=["all", "subset", "none"])
def test_cancel_discards_every_edit_and_does_not_mutate_prior(prior, cancel):
    original = None if prior is None else list(prior)
    screen = snug._MembersScreen(["first", "second", "last"], prior)
    screen.handle("a")
    screen.handle("n")
    screen.selected = 6
    screen.handle("space")
    assert screen.marked == {"last"}
    assert screen.handle(cancel) is snug._EXIT
    assert screen.members_result == original
    assert prior == original


def test_enter_on_back_discards_edits():
    prior = ["first"]
    screen = snug._MembersScreen(["first", "second"], prior)
    screen.handle("n")
    screen.selected = 5
    screen.handle("space")
    screen.selected = 3
    assert screen.handle("enter") is snug._EXIT
    assert screen.members_result == ["first"]
    assert prior == ["first"]


@pytest.mark.parametrize("name", ["folder/", "a.txt", "Café.txt", "Cafe\u0301.txt", "界🙂.txt"])
def test_member_names_remain_flat_exact_and_unicode_is_not_normalized(name):
    names = ["folder/", "folder/child.txt", "a.txt", "a.txt.more",
             "Café.txt", "Cafe\u0301.txt", "界🙂.txt"]
    screen = snug._MembersScreen(names, [])
    screen.selected = 4 + names.index(name)
    screen.handle("space")
    assert screen.marked == {name}
    assert screen.handle("confirm") is snug._EXIT
    assert screen.members_result == [name]
    assert dict(screen.options)[str(names.index(name) + 1)] == f"[x] {name}"


@pytest.mark.parametrize("size", [(40, 10), (60, 12), (80, 16), (80, 24)])
@pytest.mark.parametrize("zero_marks", [False, True], ids=["marked", "zero-notice"])
def test_member_selection_count_and_help_fit_layout(monkeypatch, size, zero_marks):
    names = [f"member-{index:02d}-界🙂e\u0301.txt" for index in range(30)]
    screen = snug._MembersScreen(names, [])
    for index in (0, 23):
        screen.selected = index + 4
        screen.handle("space")
    if zero_marks:
        screen.handle("n")
        assert screen.handle("confirm") is None
    count = 0 if zero_marks else 2
    output = io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", output)
    snug._draw_lines(screen.draw(*size), screen.footer(), size=size)
    lines = _terminal_rows(output.getvalue(), size)
    text = SGR.sub("", "\n".join(lines.values()))
    selected = [line for line in lines.values() if "❯" in line]
    assert len(selected) == 1
    assert "member-23" in selected[0]
    assert ("[ ]" if zero_marks else "[x]") in selected[0]
    assert re.search(rf"(?:Marked\s*[:(]?\s*{count}|{count}\s*(?:/\s*30\s*)?marked)",
                     text, re.IGNORECASE)
    footer = SGR.sub("", " ".join(screen.footer()))
    assert "Space" in footer and "Tab" in footer and "Esc" in footer
    assert "Space" in text and "Tab" in text and "Esc" in text
    if zero_marks:
        assert "Nothing marked" in text
    if size != (80, 24):
        assert "Use this selection" in text and "Back" in text


@pytest.mark.parametrize("prior", [None, ["tree/one.txt"]], ids=["all", "subset"])
def test_escape_from_persistent_workflow_keeps_review_selection_without_extracting(
        monkeypatch, tmp_path, quiet_tui, prior):
    archive = make_zip(tmp_path / "archive.zip")
    engine = snug.ArchiveEngine(backends=[snug.NativeBackend()])
    extract = Mock(side_effect=AssertionError("cancelled edit extracted"))
    monkeypatch.setattr(engine, "extract", extract)
    options = snug._ExtractOptions(archive, False, str(tmp_path / "output"), members=prior)
    menus = script_menus(monkeypatch, ["m", "b"])
    monkeypatch.setattr(snug.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(snug.sys.stdout, "isatty", lambda: True)

    def edit(screen):
        assert isinstance(screen, snug._MembersScreen)
        screen.handle("n")
        screen.selected = 5
        screen.handle("space")
        assert screen.marked == {"tree/two.txt"}
        assert screen.handle("esc") is snug._EXIT

    monkeypatch.setattr(snug, "_run_screen", edit)
    assert snug._configure_extract(engine, options) is False
    assert options.members == prior
    label = dict(menus[-1][1])["m"]
    assert label == ("Members: all" if prior is None else "Members: 1 exact member(s)")
    extract.assert_not_called()
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("suffix", ["zip", "tar"])
@pytest.mark.parametrize("selection", ["directory", "prefix", "unicode", "multiple"])
def test_tui_and_cli_pass_same_exact_names_and_extract_same_members(
        monkeypatch, tmp_path, quiet_tui, suffix, selection):
    archive = tmp_path / f"archive.{suffix}"
    payloads = {"dir/child.txt": b"child", "a.txt": b"short prefix",
                "a.txt.more": b"long prefix", "Cafe\u0301.txt": b"decomposed",
                "Việt.txt": b"composed"}
    write_archive(archive, [(name, "file", payload) for name, payload in payloads.items()])
    directory = "dir/" if suffix == "zip" else "dir"
    if suffix == "zip":
        with zipfile.ZipFile(archive, "a") as opened:
            opened.writestr(directory, b"")
    else:
        with tarfile.open(archive, "a") as opened:
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            opened.addfile(info)

    selected = {
        "directory": [directory], "prefix": ["a.txt"],
        "unicode": ["Cafe\u0301.txt"],
        "multiple": ["dir/child.txt", "a.txt.more", "Việt.txt"],
    }[selection]
    native = snug.ArchiveEngine(backends=[snug.NativeBackend()])
    calls = []

    class Engine:
        def inspect(self, path, **kwargs):
            return native.inspect(path, **kwargs)

        def extract(self, path, destination, **kwargs):
            calls.append((path, destination, kwargs["members"]))
            return native.extract(path, destination, **kwargs)

    engine = Engine()
    tui_destination = tmp_path / "tui-output"
    cli_destination = tmp_path / "cli-output"
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, ["d", "m", "r"])
    script_prompts(monkeypatch, [str(tui_destination)])
    monkeypatch.setattr(snug.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(snug.sys.stdout, "isatty", lambda: True)

    def edit(screen):
        assert isinstance(screen, snug._MembersScreen)
        screen.handle("n")
        for name in selected:
            screen.selected = screen.names.index(name) + 4
            screen.handle("space")
            assert calls == []
        assert screen.handle("confirm") is snug._EXIT
        assert screen.members_result == selected
        assert calls == []

    monkeypatch.setattr(snug, "_run_screen", edit)
    snug._menu_extract(engine)
    argv = ["extract", str(archive), "-C", str(cli_destination), "-q"]
    for name in selected:
        argv.extend(["--member", name])
    args = snug._build_parser().parse_args(argv)
    assert args.member == selected
    snug._cmd_extract(args, engine)

    assert calls == [(archive, str(tui_destination), selected),
                     (str(archive), str(cli_destination), selected)]
    expected_files = {name: payloads[name] for name in selected if name in payloads}
    for destination in (tui_destination, cli_destination):
        assert {path.relative_to(destination) for path in destination.rglob("*") if path.is_file()} == {
            Path(name) for name in expected_files}
        for name, payload in expected_files.items():
            assert (destination / name).read_bytes() == payload
        if selection == "directory":
            assert (destination / "dir").is_dir()
            assert not (destination / "dir/child.txt").exists()
        elif selection == "prefix":
            assert not (destination / "a.txt.more").exists()
            assert not (destination / "dir/child.txt").exists()
