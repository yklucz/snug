"""Manual archive paths through the existing builtin line input."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import snug
from test_tui_layout import _cells, _terminal_rows
from test_tui_options import make_zip, quiet_tui, script_menus


def _input_lines(monkeypatch, lines):
    values = iter(lines)
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        value = next(values)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr("builtins.input", read)
    return prompts


@pytest.mark.parametrize("text", [" leading ", " ", "\tViệt\t", "Cafe\u0301 📦 "])
def test_prompt_preserves_raw_text_only_when_requested(monkeypatch, text):
    prompts = _input_lines(monkeypatch, [text, text])

    assert snug._prompt("Archive path", preserve_spaces=True) == text
    assert snug._prompt("Other value") == text.strip()
    assert prompts == ["  Archive path: ", "  Other value: "]


@pytest.mark.parametrize("preserve", [False, True])
def test_prompt_blank_still_uses_existing_default(monkeypatch, preserve):
    prompts = _input_lines(monkeypatch, [""])

    assert snug._prompt("Other value", "default", preserve_spaces=preserve) == "default"
    assert prompts == ["  Other value [default]: "]


@pytest.mark.parametrize("name", [
    " leading.zip", "trailing.zip ", " both.zip ", " ",
    " Việt 📦.zip ", " cafe\u0301.zip ", '"quoted"\\literal.zip ',
])
def test_manual_path_accepts_literal_filename_without_trimming_or_unescaping(
        monkeypatch, tmp_path, quiet_tui, name):
    (tmp_path / name).write_bytes(b"file")
    monkeypatch.chdir(tmp_path)
    prompts = _input_lines(monkeypatch, [name])

    assert snug._handle_manual_path() == Path(name)
    assert prompts == ["  Archive path: "]


def test_manual_path_chooses_exact_file_when_stripped_file_also_exists(
        monkeypatch, tmp_path, quiet_tui, capsys):
    (tmp_path / "archive.zip").write_bytes(b"stripped")
    exact = " archive.zip "
    (tmp_path / exact).write_bytes(b"exact")
    monkeypatch.chdir(tmp_path)
    _input_lines(monkeypatch, [exact])

    selected = snug._handle_manual_path()

    assert selected == Path(exact)
    assert selected.read_bytes() == b"exact"
    assert "Not a file" not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("padding", ["leading", "trailing", "both"])
def test_manual_path_hint_does_not_accept_or_default_to_stripped_existing_path(
        monkeypatch, tmp_path, quiet_tui, capsys, kind, padding):
    stripped = tmp_path / "archive.zip"
    stripped.write_bytes(b"archive") if kind == "file" else stripped.mkdir()
    raw = {"leading": " archive.zip", "trailing": "archive.zip ",
           "both": " archive.zip "}[padding]
    monkeypatch.chdir(tmp_path)
    calls = []

    def read(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return raw
        output = capsys.readouterr().out
        assert "Not a file" in output
        assert "strip" in output.lower() and "exist" in output.lower()
        assert raw in output
        # This step retains read-only context, with a fresh empty input.
        assert prompt == "  Archive path: "
        return ""

    monkeypatch.setattr("builtins.input", read)

    assert snug._handle_manual_path() is None
    assert calls == ["  Archive path: "] * 2


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_invalid_manual_path_error_survives_until_fresh_retry_without_menu_redraw(
        monkeypatch, tmp_path, capsys, kind):
    valid = tmp_path / "valid.zip"
    valid.write_bytes(b"valid")
    invalid = " invalid.zip "
    if kind == "directory":
        (tmp_path / invalid).mkdir()
    monkeypatch.chdir(tmp_path)
    clears = Mock()
    menus = Mock(side_effect=AssertionError("invalid path returned to menu"))
    monkeypatch.setattr(snug, "_clear_screen", clears)
    monkeypatch.setattr(snug, "_select_menu", menus)
    reads = []

    def read(prompt):
        reads.append(prompt)
        if len(reads) == 1:
            return invalid
        feedback = capsys.readouterr().out
        assert "Not a file" in feedback
        assert invalid in feedback
        assert f'"{invalid}"' in feedback or repr(invalid) in feedback
        assert "strip" not in feedback.lower()
        assert "read-only" in feedback.lower()
        assert prompt == "  Archive path: "
        clears.assert_called_once_with()
        return valid.name

    monkeypatch.setattr("builtins.input", read)

    assert snug._handle_manual_path() == Path(valid.name)
    assert reads == ["  Archive path: "] * 2
    menus.assert_not_called()


def test_repeated_invalid_submissions_keep_each_rejected_value_visible(
        monkeypatch, tmp_path, quiet_tui, capsys):
    monkeypatch.chdir(tmp_path)
    values = [" first missing.zip ", " second missing.zip ", ""]
    calls = 0

    def read(prompt):
        nonlocal calls
        if calls:
            feedback = capsys.readouterr().out
            assert "Not a file" in feedback
            assert values[calls - 1] in feedback
            assert prompt == "  Archive path: "
        value = values[calls]
        calls += 1
        return value

    monkeypatch.setattr("builtins.input", read)

    assert snug._handle_manual_path() is None
    assert calls == 3


@pytest.mark.parametrize("error", [EOFError(), KeyboardInterrupt()])
@pytest.mark.parametrize("after_invalid", [False, True])
def test_manual_path_interruptions_keep_existing_quit_semantics(
        monkeypatch, tmp_path, quiet_tui, error, after_invalid):
    monkeypatch.chdir(tmp_path)
    prompts = _input_lines(monkeypatch, (["missing.zip"] if after_invalid else []) + [error])

    with pytest.raises(snug._QuitInteractive) as raised:
        snug._handle_manual_path()

    assert raised.value.__cause__ is error
    assert len(prompts) == (2 if after_invalid else 1)


@pytest.mark.parametrize("name", [" leading.zip", "trailing.zip ", " Việt 📦.zip ", " cafe\u0301.zip "])
def test_manual_exact_path_reaches_native_extraction_after_invalid_retry(
        monkeypatch, tmp_path, quiet_tui, name):
    archive = make_zip(tmp_path / name)
    destination = tmp_path / "output"
    monkeypatch.chdir(tmp_path)
    prompts = _input_lines(monkeypatch, ["missing.zip", name, str(destination)])
    script_menus(monkeypatch, ["m", "d", "r"])
    native = snug.ArchiveEngine()
    calls = []

    class Engine:
        def extract(self, path, target, **kwargs):
            calls.append((path, target))
            return native.extract(path, target, **kwargs)

    snug._menu_extract(Engine())

    assert calls == [(Path(name), str(destination))]
    assert (destination / "tree" / "one.txt").read_bytes() == b"one"
    assert (destination / "tree" / "two.txt").read_bytes() == b"second"
    assert archive.is_file()
    assert prompts[:2] == ["  Archive path: "] * 2
    assert len(prompts) == 3


def test_invalid_manual_path_then_blank_never_calls_extraction_engine(
        monkeypatch, tmp_path, quiet_tui, capsys):
    monkeypatch.chdir(tmp_path)
    _input_lines(monkeypatch, ["missing.zip", ""])
    engine = Mock(spec=snug.ArchiveEngine)

    snug._menu_extract(engine)

    assert engine.mock_calls == []
    assert "Not a file" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("long", [False, True])
def test_manual_rejection_context_has_closed_quotes_and_space_counts_within_cell_budget(
        monkeypatch, tmp_path, capsys, size, long):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(snug.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    raw = "  " + "Việt 界📦e\u0301" * (50 if long else 1) + "   "

    snug._manual_path_error(raw)

    output = capsys.readouterr().out
    rows = _terminal_rows(output, size)
    assert rows[1] == "Not a file."
    quoted = rows[2]
    assert quoted.startswith('Rejected: "') and quoted.endswith('"')
    assert any("Leading spaces: 2" in line for line in rows.values())
    assert any("Trailing spaces: 3" in line for line in rows.values())
    assert any("empty returns" in line for line in rows.values())
    assert all(_cells(line) <= size[0] - 1 for line in rows.values())
    assert len(rows) <= size[1] - 2
    if long:
        assert "…" in quoted
    else:
        assert f'"{raw}"' in quoted


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
def test_compact_and_full_stripped_hint_remain_visible_above_fresh_prompt(
        monkeypatch, tmp_path, capsys, size):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "archive.zip").touch()
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(snug.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    calls = []

    def read(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            # Ignore the initial screen setup before inspecting rejection output.
            capsys.readouterr()
            return " archive.zip "
        rows = _terminal_rows(capsys.readouterr().out, size)
        assert any("Not a file" in line for line in rows.values())
        assert any("stripped path exists" in line for line in rows.values())
        assert prompt == "  Archive path: "
        return ""

    monkeypatch.setattr("builtins.input", read)

    assert snug._handle_manual_path() is None
    assert calls == ["  Archive path: "] * 2


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
def test_long_edge_spaces_have_visible_counts_after_quoted_context_is_clipped(
        monkeypatch, tmp_path, capsys, size):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(snug.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    raw = " " * 100 + "Việt 📦 missing.zip" + " " * 101

    snug._manual_path_error(raw)

    rows = _terminal_rows(capsys.readouterr().out, size)
    assert rows[2].startswith('Rejected: "') and rows[2].endswith('"')
    assert "…" in rows[2]
    assert any("Leading spaces: 100" in line for line in rows.values())
    assert any("Trailing spaces: 101" in line for line in rows.values())
    assert any("read-only" in line for line in rows.values())
    assert any("empty returns" in line for line in rows.values())
    assert all(_cells(line) <= size[0] - 1 for line in rows.values())
