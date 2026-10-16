"""Creation collection must retain metadata failures as visible omissions."""

import errno
import os
from pathlib import Path

import pytest

import snug_core as core


@pytest.mark.parametrize("symlinks", ["store", "follow"])
@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO])
def test_unreadable_top_level_metadata_is_skipped_without_losing_other_inputs(
        tmp_path, monkeypatch, symlinks, number):
    kept = tmp_path / "kept.txt"
    denied = tmp_path / "denied.txt"
    kept.write_bytes(b"kept payload")
    denied.write_bytes(b"omitted payload")
    original = Path.lstat

    def inaccessible(path, *args, **kwargs):
        if path == denied:
            raise OSError(number, os.strerror(number), str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", inaccessible)
    archive = tmp_path / "result.zip"
    engine = core.ArchiveEngine([core.NativeBackend()])
    report = engine.create(archive, [kept, denied], symlinks=symlinks)
    assert report.included_inputs == 1 and report.accidental_skips == 1
    assert report.skip_errors[0].path == denied
    assert os.strerror(number) in report.skip_errors[0].reason
    output = tmp_path / "output"
    engine.extract(archive, output)
    assert (output / "kept.txt").read_bytes() == b"kept payload"
    assert not (output / "denied.txt").exists()


@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO])
def test_unreadable_child_metadata_does_not_stop_remaining_directory_items(
        tmp_path, monkeypatch, number):
    tree = tmp_path / "tree"
    tree.mkdir()
    denied = tree / "a-denied.txt"
    kept = tree / "b-kept.txt"
    denied.write_bytes(b"omitted payload")
    kept.write_bytes(b"kept payload")
    original = Path.lstat

    def inaccessible(path, *args, **kwargs):
        if path == denied:
            raise OSError(number, os.strerror(number), str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", inaccessible)
    archive = tmp_path / "result.tar"
    engine = core.ArchiveEngine([core.NativeBackend()])
    report = engine.create(archive, [tree], root=tree)
    assert report.included_inputs == 1 and report.accidental_skips == 1
    assert report.skip_errors[0].path == denied
    output = tmp_path / "output"
    engine.extract(archive, output)
    assert (output / "b-kept.txt").read_bytes() == b"kept payload"
    assert not (output / "a-denied.txt").exists()


@pytest.mark.parametrize("symlinks", ["store", "follow"])
def test_standalone_source_metadata_error_reports_all_skipped_and_no_output(
        tmp_path, monkeypatch, symlinks):
    denied = tmp_path / "denied.txt"
    denied.write_bytes(b"omitted payload")
    original = Path.lstat

    def inaccessible(path, *args, **kwargs):
        if path == denied:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", inaccessible)
    archive = tmp_path / "result.gz"
    with pytest.raises(core.CreateError) as failure:
        core.ArchiveEngine([core.StreamBackend()]).create(archive, [denied], symlinks=symlinks)
    assert failure.value.report.included_inputs == 0
    assert failure.value.report.accidental_skips == 1
    assert failure.value.report.skip_errors[0].path == denied
    assert not archive.exists()
    assert not list(tmp_path.glob(".result.gz.*.part"))


@pytest.mark.parametrize("method", ["stat", "lstat"])
def test_best_effort_create_size_never_raises_metadata_error(tmp_path, monkeypatch, method):
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    original = getattr(Path, method)

    def inaccessible(path, *args, **kwargs):
        if path == source:
            raise OSError(errno.EIO, "Input/output error", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, inaccessible)
    assert core._file_size(source) == 0
