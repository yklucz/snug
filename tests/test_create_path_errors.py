"""Metadata failures must omit inputs consistently across Python versions."""

import errno
import os
from pathlib import Path

import pytest

import snug_core as core
import snug_ext as ext


@pytest.mark.parametrize("symlinks", ["store", "follow"])
@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO, errno.ENAMETOOLONG])
@pytest.mark.parametrize("case", ["partial", "all", "existing-partial", "existing-all"])
def test_ar_prevalidation_metadata_error_obeys_publication_policy(
        tmp_path, monkeypatch, libarchive_backend, symlinks, number, case):
    kept = tmp_path / "kept.txt"
    denied = tmp_path / "denied.txt"
    kept.write_bytes(b"kept payload")
    denied.write_bytes(b"omitted payload")
    archive = tmp_path / "result.ar"
    existing = case.startswith("existing")
    all_skipped = case.endswith("all")
    if existing:
        archive.write_bytes(b"existing archive bytes")
    method = "stat" if symlinks == "follow" else "lstat"
    original = getattr(Path, method)

    def inaccessible(path, *args, **kwargs):
        if path == denied:
            raise OSError(number, os.strerror(number), str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, inaccessible)
    items = [(denied, denied.name)] if all_skipped else [
        (kept, kept.name), (denied, denied.name)]
    engine = core.ArchiveEngine([ext.LibarchiveBackend()])
    if case == "partial":
        report = engine.create(archive, [], symlinks=symlinks, precollected=(items, 0))
        output = tmp_path / "output"
        engine.extract(archive, output)
        assert (output / "kept.txt").read_bytes() == b"kept payload"
        assert not (output / "denied.txt").exists()
    else:
        with pytest.raises(core.CreateError) as failure:
            engine.create(archive, [], symlinks=symlinks, precollected=(items, 0))
        report = failure.value.report
        assert failure.value.preserved_existing == existing
        assert archive.read_bytes() == b"existing archive bytes" if existing else not archive.exists()
    assert report.included_inputs == (0 if all_skipped else 1)
    assert report.accidental_skips == 1
    assert report.skip_errors[0].path == denied
    assert os.strerror(number) in report.skip_errors[0].reason
    assert not list(tmp_path.glob(".result.ar.*.part"))


@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO, errno.ENAMETOOLONG])
@pytest.mark.parametrize("all_skipped", [False, True])
def test_sevenzip_stored_symlink_target_metadata_error_is_accidental(
        tmp_path, monkeypatch, py7zr_backend, number, all_skipped):
    kept = tmp_path / "kept.txt"
    kept.write_bytes(b"kept payload")
    target = tmp_path / "target.txt"
    target.write_bytes(b"link payload")
    link = tmp_path / "link"
    link.symlink_to(target)
    original = Path.stat

    def unavailable_target(path, *args, **kwargs):
        if path == link and kwargs.get("follow_symlinks", True):
            raise OSError(number, os.strerror(number), str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", unavailable_target)
    items = [(link, "link")] if all_skipped else [(kept, "kept.txt"), (link, "link")]
    archive = tmp_path / "result.7z"
    engine = core.ArchiveEngine([ext.SevenZipBackend()])
    if all_skipped:
        with pytest.raises(core.CreateError) as failure:
            engine.create(archive, [], precollected=(items, 0))
        report = failure.value.report
        assert not archive.exists()
    else:
        report = engine.create(archive, [], precollected=(items, 0))
        output = tmp_path / "output"
        engine.extract(archive, output)
        assert (output / "kept.txt").read_bytes() == b"kept payload"
        assert not (output / "link").exists()
    assert report.included_inputs == (0 if all_skipped else 1)
    assert report.accidental_skips == 1
    assert report.skip_errors[0].path == link
    assert os.strerror(number) in report.skip_errors[0].reason
    assert not list(tmp_path.glob(".result.7z.*.part"))


@pytest.mark.parametrize("target", ["missing", "dangling"])
def test_sevenzip_genuinely_dangling_stored_symlink_still_aborts(
        tmp_path, py7zr_backend, target):
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / target)
    archive = tmp_path / "result.7z"
    with pytest.raises(core.ArchiveError, match="dangling symbolic link"):
        core.ArchiveEngine([ext.SevenZipBackend()]).create(
            archive, [], precollected=([(link, "dangling")], 0))
    assert not archive.exists()
    assert not list(tmp_path.glob(".result.7z.*.part"))


@pytest.mark.parametrize("symlinks", ["store", "follow"])
@pytest.mark.parametrize("all_skipped", [False, True])
def test_ar_cli_late_metadata_failure_warns_with_partial_or_empty_exit(
        tmp_path, monkeypatch, capsys, libarchive_backend, symlinks, all_skipped):
    import snug

    kept = tmp_path / "kept.txt"
    denied = tmp_path / "denied.txt"
    kept.write_bytes(b"kept payload")
    denied.write_bytes(b"omitted payload")
    archive = tmp_path / "result.ar"
    collected = False
    collect = core.ArchiveEngine._collect_items

    def collect_before_failure(*args, **kwargs):
        nonlocal collected
        result = collect(*args, **kwargs)
        collected = True
        return result

    method = "stat" if symlinks == "follow" else "lstat"
    original = getattr(Path, method)

    def unreadable_after_collection(path, *args, **kwargs):
        if collected and path == denied:
            raise OSError(errno.EIO, "Input/output error", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(core.ArchiveEngine, "_collect_items", staticmethod(collect_before_failure))
    monkeypatch.setattr(Path, method, unreadable_after_collection)
    monkeypatch.setattr(snug, "_start_update_check", lambda: None)
    selected = [denied] if all_skipped else [kept, denied]
    code = snug.main(["create", str(archive), *map(str, selected), "--symlinks", symlinks, "-q"])
    output = capsys.readouterr()
    assert code == (2 if all_skipped else 5)
    assert output.out == ""
    assert "warning:" in output.err and str(denied) in output.err
    assert "input/output error" in output.err.lower()
    assert "Traceback" not in output.err
    assert archive.exists() != all_skipped
    assert not list(tmp_path.glob(".result.ar.*.part"))


@pytest.mark.parametrize("kind", ["directory", "stored-link", "nested-name", "backslash-name"])
def test_ar_prevalidation_still_rejects_nonregular_or_nonflat_input(
        tmp_path, libarchive_backend, kind):
    source = tmp_path / "source"
    name = "source"
    if kind == "directory":
        source.mkdir()
    elif kind == "stored-link":
        target = tmp_path / "target"
        target.write_bytes(b"payload")
        source.symlink_to(target)
    else:
        source.write_bytes(b"payload")
        name = "nested/source" if kind == "nested-name" else "nested\\source"
    archive = tmp_path / "result.ar"
    with pytest.raises(core.ArchiveError, match="regular files with flat archive names"):
        core.ArchiveEngine([ext.LibarchiveBackend()]).create(
            archive, [], precollected=([(source, name)], 0))
    assert not archive.exists()
    assert not list(tmp_path.glob(".result.ar.*.part"))


def test_ar_followed_symlink_keeps_regular_file_payload(tmp_path, libarchive_backend):
    target = tmp_path / "target"
    target.write_bytes(b"followed payload")
    link = tmp_path / "link"
    link.symlink_to(target)
    archive = tmp_path / "result.ar"
    engine = core.ArchiveEngine([ext.LibarchiveBackend()])
    report = engine.create(archive, [link], symlinks="follow")
    assert report.files == 1 and report.accidental_skips == 0
    output = tmp_path / "output"
    engine.extract(archive, output)
    assert (output / "link").read_bytes() == b"followed payload"
    assert not (output / "link").is_symlink()
