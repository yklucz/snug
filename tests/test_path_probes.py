"""Old pathlib probe errors stay local to input validation."""

import errno
from pathlib import Path
from unittest.mock import Mock

import pytest

import snug
from test_tui_options import quiet_tui


def fail_probe(monkeypatch, method, paths, code=errno.ENAMETOOLONG):
    original = getattr(Path, method)

    def probe(path, *args, **kwargs):
        if path in paths:
            raise OSError(code, "probe failed", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, probe)


@pytest.mark.parametrize("code", [errno.ENAMETOOLONG, errno.EACCES])
def test_manual_file_probe_rejects_and_reopens_exact_draft(monkeypatch, tmp_path, quiet_tui, code):
    path = tmp_path / "literal archive.zip"
    calls = []
    fail_probe(monkeypatch, "is_file", {path}, code)

    def prompt(draft, context):
        calls.append((draft, context))
        return str(path) if len(calls) == 1 else ""

    monkeypatch.setattr(snug, "_prompt_manual_archive_path", prompt)
    assert snug._handle_manual_path() is None
    assert calls[1][0] == str(path)
    assert "Not a file." in calls[1][1]
    if code == errno.ENAMETOOLONG:
        assert any("file name or path is too long" in line for line in calls[1][1])
        assert any("shorter" in line for line in calls[1][1])


@pytest.mark.parametrize("which", ["raw", "stripped"])
def test_manual_rejection_hint_probe_error_is_bounded_and_has_advice(monkeypatch, tmp_path, which):
    monkeypatch.chdir(tmp_path)
    raw = " missing.zip "
    fail_probe(monkeypatch, "exists", {Path(raw if which == "raw" else raw.strip())})
    lines = snug._manual_path_error(raw)
    assert "Not a file." in lines
    assert not any("Hint: the stripped path exists." in line for line in lines)
    assert any("file name or path is too long" in line for line in lines)


@pytest.mark.parametrize("which", ["remembered", "candidate", "parent"])
def test_browse_start_probe_failure_uses_usable_fallback(monkeypatch, tmp_path, which):
    monkeypatch.chdir(tmp_path)
    remembered = tmp_path / "remembered"
    remembered.mkdir()
    candidate = tmp_path / "parent" / "archive.zip"
    candidate.parent.mkdir()
    raw = "" if which == "remembered" else str(candidate)
    monkeypatch.setattr(snug, "_ARCHIVE_BROWSE_DIRECTORY", remembered)
    failed = {"remembered": remembered, "candidate": candidate, "parent": candidate.parent}[which]
    fail_probe(monkeypatch, "is_dir", {failed})
    expected = candidate.parent if which == "candidate" else tmp_path
    assert snug._archive_browse_start(raw) == expected


def test_archive_discovery_skips_only_candidate_with_failed_file_probe(monkeypatch, tmp_path):
    bad, good = tmp_path / "bad.zip", tmp_path / "good.zip"
    bad.touch()
    good.touch()
    fail_probe(monkeypatch, "is_file", {bad})
    assert snug._find_archives(tmp_path) == [good]


@pytest.mark.parametrize("method", ["is_dir", "is_symlink"])
def test_picker_enterable_probe_failure_is_not_enterable(monkeypatch, tmp_path, method):
    child = tmp_path / "child"
    child.mkdir()
    fail_probe(monkeypatch, method, {child})
    assert not snug._is_enterable(child)


@pytest.mark.parametrize("method", ["is_dir", "is_symlink"])
@pytest.mark.parametrize("render", ["name", "row"])
def test_picker_display_probe_failure_does_not_raise(monkeypatch, tmp_path, method, render):
    child = tmp_path / "child"
    child.mkdir()
    screen = snug._PickerScreen(tmp_path)
    fail_probe(monkeypatch, method, {child})
    text = snug._entry_name(child) if render == "name" else screen._render_entry(0, child)
    assert "child" in text


@pytest.mark.parametrize("method", ["is_dir", "exists", "is_symlink"])
def test_picker_navigation_probe_error_does_not_enter(monkeypatch, tmp_path, method):
    child = tmp_path / "child"
    child.mkdir()
    screen = snug._PickerScreen(tmp_path)
    if method == "exists":
        child.rmdir()  # Exercise the remembered, disappeared-directory branch.
    fail_probe(monkeypatch, method, {child})
    assert not screen._can_enter(child)


@pytest.mark.parametrize("method", ["is_dir", "is_symlink"])
def test_single_picker_failed_probe_cannot_accept_directory(monkeypatch, tmp_path, method):
    child = tmp_path / "child"
    child.mkdir()
    screen = snug._PickerScreen(tmp_path, single=True)
    fail_probe(monkeypatch, method, {child})
    assert not screen._confirm()
    assert screen.result is None
    assert "file name or path is too long" in screen._notice


@pytest.mark.parametrize("code", [errno.ENAMETOOLONG, errno.EACCES])
def test_tui_failed_overwrite_probe_cannot_authorize_creation(monkeypatch, tmp_path, quiet_tui, code):
    archive = tmp_path / "existing.zip"
    archive.write_bytes(b"existing")
    source = tmp_path / "source"
    source.touch()
    engine = Mock()
    engine.writable_formats.return_value = [snug.ArchiveFormat.ZIP]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda *a, **k: [source])

    def configure(options):
        options.archive_path = archive
        return True

    monkeypatch.setattr(snug, "_configure_create", configure)
    fail_probe(monkeypatch, "exists", {archive}, code)
    with pytest.raises(snug.ArchiveError, match="existing.zip"):
        snug._menu_create(engine)
    engine.create.assert_not_called()
    assert archive.read_bytes() == b"existing"


@pytest.mark.parametrize("code", [errno.ENAMETOOLONG, errno.EACCES])
def test_cli_failed_overwrite_probe_keeps_existing_error_and_output(monkeypatch, tmp_path, capsys, code):
    archive = tmp_path / "existing.zip"
    archive.write_bytes(b"existing")
    engine = Mock()
    monkeypatch.setattr(snug, "ArchiveEngine", lambda: engine)
    monkeypatch.setattr(snug, "_start_update_check", lambda: None)
    fail_probe(monkeypatch, "exists", {archive}, code)
    assert snug.main(["create", str(archive), str(tmp_path / "source")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ") and str(archive) in captured.err
    assert ("too long" if code == errno.ENAMETOOLONG else "permission denied") in captured.err
    engine.create.assert_not_called()
    assert archive.read_bytes() == b"existing"
