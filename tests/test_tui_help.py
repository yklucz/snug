"""Help survives the supported viewports and describes current cancellation."""

import io
from types import SimpleNamespace

import pytest

import snug
from test_tui_editor import tty_prompt
from test_tui_layout import _cells, _terminal_rows


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("kind", [
    "main", "option", "sources", "sources-filter", "sources-empty-filter",
    "archive", "archive-filter", "archive-empty-filter", "members",
    "members-empty", "limits", "field-e", "field-s", "field-f", "field-r",
    "result", "error",
])
def test_help_footers_survive_rendering_without_clipping(tmp_path, monkeypatch, size, kind):
    (tmp_path / "sample.txt").touch()
    if kind == "main":
        screen = snug._MenuScreen("Main", None, snug._MENU_OPTIONS)
        screen.cancel_on_interrupt = False
    elif kind == "option":
        screen = snug._MenuScreen("Options", None, [("r", "Run"), ("b", "Back")])
    elif kind.startswith(("sources", "archive")):
        screen = snug._PickerScreen(tmp_path, single=kind.startswith("archive"))
        if kind.endswith("filter"):
            screen.handle("/")
            if "empty" not in kind:
                screen.handle("s")
    elif kind.startswith("members"):
        screen = snug._MembersScreen(["a.txt", "b.txt"], None)
        if kind == "members-empty":
            screen.handle("n")
            screen.handle("confirm")
    elif kind == "limits" or kind.startswith("field"):
        screen = snug._LimitsScreen(snug.ExtractionLimits())
        if kind.startswith("field"):
            screen.handle(kind[-1])
    else:
        screen = snug._PauseScreen(["error: rejected archive" if kind == "error" else "Done"])

    output = io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", output)
    body = screen.draw(*size)
    footer = screen.footer()
    snug._draw_lines(body, footer, size=size)
    rendered = _terminal_rows(output.getvalue(), size)

    assert all(_cells(line) <= size[0] - 1 for line in footer)
    for row, line in zip(range(size[1] - len(footer) + 1, size[1] + 1), footer):
        assert rendered.get(row, "") == snug._TUI_SGR.sub("", line)
    assert "Ctrl+C" in "\n".join(footer)


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("main", [False, True])
def test_menu_help_distinguishes_local_cancel_from_main_quit(size, main):
    screen = snug._MenuScreen("Menu", None, snug._MENU_OPTIONS)
    screen.cancel_on_interrupt = not main
    screen.draw(*size)
    help_text = "\n".join(screen.footer())
    assert "130" in help_text if main else "back" in help_text and "130" not in help_text


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
def test_short_compression_label_preserves_history_and_complete_range_hint(
        tty_prompt, monkeypatch, size):
    output, _ = tty_prompt(["up", "enter"])
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    snug._PROMPT_HISTORY["Compression level (0-9 or default)"] = ["7"]
    assert snug._menu_compression(None) == 7
    visible = "\n".join(_terminal_rows(output.getvalue(), size).values())
    assert "Compression (0-9/default) [default]:" in visible
    assert "Compression (0-9/default)" not in snug._PROMPT_HISTORY


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
def test_rejected_path_help_matches_the_editable_prefill(tty_prompt, monkeypatch, tmp_path, size):
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    raw = "missing.zip"
    checked = []

    def retry(output):
        visible = "\n".join(_terminal_rows(output.getvalue(), size).values())
        assert "Edit the path below." in visible and "> " + raw in visible
        assert "read-only" not in visible
        checked.append(True)
        return "esc"

    tty_prompt([*raw, "enter", retry])
    monkeypatch.chdir(tmp_path)
    assert snug._handle_manual_path() is None
    assert checked == [True]


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("kind", ["source-empty", "member-empty", "directory-link"])
def test_selection_guidance_is_complete_at_supported_sizes(tmp_path, monkeypatch, size, kind):
    target = tmp_path / "folder"
    target.mkdir()
    (tmp_path / "link").symlink_to(target, target_is_directory=True)
    if kind == "member-empty":
        screen = snug._MembersScreen(["member"], None)
        screen.handle("n")
    else:
        screen = snug._PickerScreen(tmp_path, single=kind == "directory-link")
        if kind == "directory-link":
            screen.cursor = screen.entries.index(tmp_path / "link")
    screen.handle("confirm")
    output = io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", output)
    snug._draw_lines(screen.draw(*size), screen.footer(), size=size)
    rendered = _terminal_rows(output.getvalue(), size)
    assert any(line == "  " + screen._notice for line in rendered.values())


def test_non_tty_rejection_and_compression_messages_are_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(snug.sys.stdout, "isatty", lambda: False)
    snug._manual_path_error("missing.zip")
    assert "Rejected text is read-only." in capsys.readouterr().out
    replies = iter(["wrong", "default"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))
    assert snug._menu_compression(None) is None
    assert "Enter a compression level from 0 to 9, or default." in capsys.readouterr().out
