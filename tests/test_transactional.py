"""Independent archive fixtures and failure probes for regular-file commits."""

import bz2
import errno
import gzip
import io
import lzma
import os
from pathlib import Path
import stat
import tarfile
import zipfile

import pytest

import snug_core as core


PAYLOAD = b"completed archive member\n" * 110000
OLD = b"previous destination survives"
FORMATS = ["zip", "tar", "tar.gz", "gz", "bz2", "xz", "lzma"]


def independent_archive(tmp_path, suffix, content=PAYLOAD, name="payload.txt"):
    archive = tmp_path / (f"{name}.{suffix}" if suffix in {"gz", "bz2", "xz", "lzma"}
                          else f"archive.{suffix}")
    if suffix == "zip":
        member = zipfile.ZipInfo(name, date_time=(2001, 2, 3, 4, 5, 6))
        member.create_system = 3
        member.external_attr = (stat.S_IFREG | 0o640) << 16
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
            handle.writestr(member, content)
    elif suffix in {"tar", "tar.gz"}:
        member = tarfile.TarInfo(name)
        member.size, member.mode, member.mtime = len(content), 0o640, 981173106
        with tarfile.open(archive, "w:gz" if suffix == "tar.gz" else "w") as handle:
            handle.addfile(member, io.BytesIO(content))
    else:
        codec = {"gz": gzip, "bz2": bz2, "xz": lzma, "lzma": lzma}[suffix]
        options = {"format": lzma.FORMAT_ALONE} if suffix == "lzma" else {}
        archive.write_bytes(codec.compress(content, **options))
    return archive


def destination(tmp_path, *, existing=True, name="payload.txt"):
    root = tmp_path / "output"
    root.mkdir()
    target = root / name
    if existing:
        target.write_bytes(OLD)
    return root, target


def assert_no_temporary_files(root):
    leftovers = [path for path in root.rglob("*")
                 if ".snug-part-" in path.name or path.name.endswith(".part")]
    assert leftovers == []


@pytest.mark.parametrize("failure", [core.ArchiveError("payload CRC failure"), KeyboardInterrupt()])
def test_cleanup_failure_attempts_all_outputs_and_preserves_primary(
    engine, tmp_path, monkeypatch, failure,
):
    archive = tmp_path / "multiple.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("first.txt", b"first complete member")
        handle.writestr("second.txt", b"second complete member")
    backend = core.NativeBackend()
    extraction = backend.extract
    abort = core.SafeOutputFile.abort
    attempted = []
    failed_cleanup = []

    def fail_after_staging(path, ctx, password=None):
        extraction(path, ctx, password=password)
        raise failure

    def abort_with_failure(output):
        attempted.append(output.entry.name)
        abort(output)
        if not failed_cleanup:
            failed_cleanup.append(True)
            raise OSError("injected close failure after its unlink")

    monkeypatch.setattr(backend, "extract", fail_after_staging)
    monkeypatch.setattr(core.SafeOutputFile, "abort", abort_with_failure)
    engine = core.ArchiveEngine([backend])
    with pytest.raises(type(failure)) as caught:
        engine.extract(archive, tmp_path / "output")
    assert caught.value is failure
    assert attempted == ["first.txt", "second.txt"]
    assert not list((tmp_path / "output").iterdir())


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(7)])
@pytest.mark.parametrize("decoder_failure", [False, True])
def test_cleanup_interruption_propagates_after_all_outputs_are_attempted(
    tmp_path, monkeypatch, interruption, decoder_failure,
):
    archive = tmp_path / "multiple.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("first.txt", b"first complete member")
        handle.writestr("second.txt", b"second complete member")
    backend = core.NativeBackend()
    extraction = backend.extract
    abort = core.SafeOutputFile.abort
    attempted = []

    def extract(path, ctx, password=None):
        extraction(path, ctx, password=password)
        if decoder_failure:
            raise core.ArchiveError("injected decoder failure")

    def interrupt_first_cleanup(output):
        attempted.append(output.entry.name)
        abort(output)
        if len(attempted) == 1:
            raise interruption

    monkeypatch.setattr(backend, "extract", extract)
    monkeypatch.setattr(core.SafeOutputFile, "abort", interrupt_first_cleanup)
    engine = core.ArchiveEngine([backend])
    root = tmp_path / "output"
    with pytest.raises(type(interruption)) as caught:
        engine.extract(archive, root)
    assert caught.value is interruption
    assert attempted == ["first.txt", "second.txt"]
    assert_no_temporary_files(root)
    if decoder_failure:
        assert not list(root.iterdir())


def test_cleanup_only_failure_is_reported_after_all_attempts(engine, tmp_path, monkeypatch):
    archive = tmp_path / "multiple.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("first.txt", b"first complete member")
        handle.writestr("second.txt", b"second complete member")
    abort = core.SafeOutputFile.abort
    attempted = []

    def abort_with_failure(output):
        attempted.append(output.entry.name)
        abort(output)
        if len(attempted) == 1:
            raise OSError("injected staging cleanup failure")

    monkeypatch.setattr(core.SafeOutputFile, "abort", abort_with_failure)
    with pytest.raises(core.ArchiveError, match="cannot clean extraction staging for 'first.txt'") as caught:
        engine.extract(archive, tmp_path / "output")
    assert isinstance(caught.value.__cause__, OSError)
    assert attempted == ["first.txt", "second.txt"]
    assert (tmp_path / "output/first.txt").read_bytes() == b"first complete member"
    assert (tmp_path / "output/second.txt").read_bytes() == b"second complete member"
    assert_no_temporary_files(tmp_path / "output")


@pytest.mark.parametrize("suffix", FORMATS)
@pytest.mark.parametrize("content", [b"", PAYLOAD], ids=["empty", "streamed"])
def test_completed_regular_file_replaces_old_content(engine, tmp_path, suffix, content):
    archive = independent_archive(tmp_path, suffix, content)
    root, target = destination(tmp_path)
    report = engine.extract(archive, root)
    assert target.read_bytes() == content
    assert report.files == 1
    assert report.bytes_written == len(content)
    assert_no_temporary_files(root)


@pytest.mark.parametrize("existing", [False, True])
def test_corrupt_zip_crc_never_exposes_partial_final_file(engine, tmp_path, existing):
    archive = independent_archive(tmp_path, "zip")
    with zipfile.ZipFile(archive) as handle:
        member = handle.getinfo("payload.txt")
        payload_offset = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
    damaged = bytearray(archive.read_bytes())
    damaged[payload_offset + core.CHUNK_SIZE + 31] ^= 0xFF
    archive.write_bytes(damaged)
    root, target = destination(tmp_path, existing=existing)
    with pytest.raises(core.ArchiveError, match="CRC|crc"):
        engine.extract(archive, root)
    if existing:
        assert target.read_bytes() == OLD
    else:
        assert not target.exists()
    assert_no_temporary_files(root)


@pytest.mark.parametrize("suffix", ["zip", "tar", "gz", "xz", "lzma"])
def test_truncated_archive_preserves_destination_and_cleans_staging(engine, tmp_path, suffix):
    archive = independent_archive(tmp_path, suffix)
    data = archive.read_bytes()
    if suffix == "zip":
        # Removing only the central directory can leave a fully readable
        # streaming ZIP; truncate the member payload itself instead.
        data = data[:30 + len("payload.txt") + len(PAYLOAD) // 2]
    elif suffix == "tar":
        data = data[:512 + len(PAYLOAD) // 2]
    else:
        data = data[:-min(32, len(data) // 4)]
    archive.write_bytes(data)
    root, target = destination(tmp_path)
    with pytest.raises(core.ArchiveError):
        engine.extract(archive, root)
    assert target.read_bytes() == OLD
    assert_no_temporary_files(root)


def test_corrupt_compressed_tar_preserves_destination(engine, tmp_path):
    archive = independent_archive(tmp_path, "tar.gz")
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    archive.write_bytes(data)
    root, target = destination(tmp_path)
    with pytest.raises(core.ArchiveError):
        engine.extract(archive, root)
    assert target.read_bytes() == OLD
    assert_no_temporary_files(root)


def test_zip_missing_central_directory_is_rejected_without_tolerant_fallback(engine, tmp_path):
    archive = independent_archive(tmp_path, "zip")
    # Retain every payload byte, but remove the end-of-central-directory
    # record. A streaming optional reader must not mask this invalid ZIP.
    archive.write_bytes(archive.read_bytes()[:-32])
    root, target = destination(tmp_path)
    with pytest.raises(core.ArchiveError):
        engine.extract(archive, root)
    assert target.read_bytes() == OLD
    assert_no_temporary_files(root)


@pytest.mark.parametrize("failure", ["write", "interrupt"])
@pytest.mark.parametrize("suffix", ["zip", "tar", "gz"])
def test_output_failure_after_real_bytes_preserves_old_file(
    engine, tmp_path, monkeypatch, suffix, failure,
):
    archive = independent_archive(tmp_path, suffix)
    root, target = destination(tmp_path)
    write = core.SafeOutputFile.write
    writes = []

    def fail_after_write(output, data):
        writes.append(len(data))
        write(output, data)
        if failure == "interrupt":
            raise KeyboardInterrupt()
        raise OSError("simulated disk write failure")

    monkeypatch.setattr(core.SafeOutputFile, "write", fail_after_write)
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else core.ArchiveError):
        engine.extract(archive, root)
    assert writes and writes[0] > 0
    assert target.read_bytes() == OLD
    assert_no_temporary_files(root)


def test_backend_failure_after_finished_payload_does_not_commit(engine, tmp_path, monkeypatch):
    archive = independent_archive(tmp_path, "zip")
    root, target = destination(tmp_path)
    extract = core.NativeBackend.extract

    def fail_after_payload(backend, path, ctx, password=None):
        extract(backend, path, ctx, password=password)
        raise core.ArchiveError("simulated backend validation failure")

    monkeypatch.setattr(core.NativeBackend, "extract", fail_after_payload)
    with pytest.raises(core.ArchiveError, match="validation failure"):
        engine.extract(archive, root)
    assert target.read_bytes() == OLD
    assert_no_temporary_files(root)


@pytest.mark.parametrize("existing", [False, True])
def test_final_path_contains_only_old_or_completed_content(engine, tmp_path, monkeypatch, existing):
    archive = independent_archive(tmp_path, "zip")
    root, target = destination(tmp_path, existing=existing)
    write, commit = core.SafeOutputFile.write, core.SafeOutputFile.commit
    observed = []

    def observe_write(output, data):
        if existing:
            assert target.read_bytes() == OLD
        else:
            assert not target.exists()
        return write(output, data)

    def observe_commit(output):
        if existing:
            assert target.read_bytes() == OLD
        else:
            assert not target.exists()
        result = commit(output)
        observed.append(target.read_bytes())
        return result

    monkeypatch.setattr(core.SafeOutputFile, "write", observe_write)
    monkeypatch.setattr(core.SafeOutputFile, "commit", observe_commit)
    engine.extract(archive, root)
    assert observed == [PAYLOAD]
    assert_no_temporary_files(root)


@pytest.mark.parametrize("suffix", ["zip", "tar", "gz"])
def test_archive_close_failure_cleans_finished_staging_before_publication(
    engine, tmp_path, monkeypatch, suffix,
):
    archive = independent_archive(tmp_path, suffix)
    root, target = destination(tmp_path)
    owner = {"zip": zipfile.ZipFile, "tar": tarfile.TarFile, "gz": gzip.GzipFile}[suffix]
    close = owner.close
    failures = []

    def fail_close_after_payload(handle):
        result = close(handle)
        if not failures and any(".snug-part-" in path.name for path in root.iterdir()):
            failures.append(True)
            raise OSError("simulated archive close validation failure")
        return result

    monkeypatch.setattr(owner, "close", fail_close_after_payload)
    with pytest.raises(core.ArchiveError):
        engine.extract(archive, root)
    assert failures
    assert target.read_bytes() == OLD
    assert_no_temporary_files(root)


def test_secure_temporary_creation_uses_exclusive_flags(engine, tmp_path, monkeypatch):
    archive = independent_archive(tmp_path, "zip", b"secure staging")
    root, target = destination(tmp_path)
    open_file = core.os.open
    seen = []

    def observe_open(path, flags, *args, **kwargs):
        name = Path(os.fsdecode(path)).name
        if ".snug-part-" in name:
            seen.append(flags)
            assert flags & os.O_CREAT
            assert flags & os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                assert flags & os.O_NOFOLLOW
        return open_file(path, flags, *args, **kwargs)

    monkeypatch.setattr(core.os, "open", observe_open)
    engine.extract(archive, root)
    assert seen
    assert target.read_bytes() == b"secure staging"
    assert_no_temporary_files(root)


def test_best_effort_fsync_failure_does_not_discard_valid_payload(engine, tmp_path, monkeypatch):
    archive = independent_archive(tmp_path, "zip", b"validated")
    root, target = destination(tmp_path)
    calls = []

    def unavailable_fsync(fd):
        calls.append(fd)
        raise OSError(errno.EINVAL, "filesystem does not support fsync")

    monkeypatch.setattr(core.os, "fsync", unavailable_fsync)
    engine.extract(archive, root)
    assert calls
    assert target.read_bytes() == b"validated"
    assert_no_temporary_files(root)


def test_locked_or_failed_atomic_replacement_preserves_old_file(engine, tmp_path, monkeypatch):
    archive = independent_archive(tmp_path, "zip")
    root, target = destination(tmp_path)
    replace = core.os.replace

    def fail_target_replace(source, final, *args, **kwargs):
        if Path(final) == target:
            raise PermissionError("destination is locked by another process")
        return replace(source, final, *args, **kwargs)

    monkeypatch.setattr(core.os, "replace", fail_target_replace)
    with pytest.raises(core.ArchiveError):
        engine.extract(archive, root)
    assert target.read_bytes() == OLD
    assert_no_temporary_files(root)


@pytest.mark.parametrize("preserve", [False, True])
@pytest.mark.parametrize("suffix", ["zip", "tar"])
def test_file_metadata_is_applied_only_to_completed_final_content(
    engine, tmp_path, monkeypatch, suffix, preserve,
):
    archive = independent_archive(tmp_path, suffix, b"metadata payload")
    root, target = destination(tmp_path)
    apply_metadata = core._apply_metadata
    enabled_calls = []

    def observe_metadata(path, mode, mtime, enabled):
        if enabled:
            assert Path(path) == target
            assert target.read_bytes() == b"metadata payload"
            enabled_calls.append((mode, mtime))
        return apply_metadata(path, mode, mtime, enabled)

    monkeypatch.setattr(core, "_apply_metadata", observe_metadata)
    engine.extract(archive, root, preserve_metadata=preserve)
    assert bool(enabled_calls) is preserve
    assert target.read_bytes() == b"metadata payload"
    assert_no_temporary_files(root)


def test_metadata_failure_after_commit_keeps_completed_payload(engine, tmp_path, monkeypatch):
    archive = independent_archive(tmp_path, "zip", b"content remains complete")
    root, target = destination(tmp_path)
    chmod = core.os.chmod
    failures = []

    def unavailable_permissions(path, *args, **kwargs):
        if Path(path) == target:
            assert target.read_bytes() == b"content remains complete"
            failures.append(True)
            raise PermissionError("filesystem does not allow restoring archived permissions")
        return chmod(path, *args, **kwargs)

    monkeypatch.setattr(core.os, "chmod", unavailable_permissions)
    report = engine.extract(archive, root)
    assert failures
    assert report.files == 1
    assert target.read_bytes() == b"content remains complete"
    assert_no_temporary_files(root)


def test_later_commit_failure_preserves_earlier_completed_file(engine, tmp_path, monkeypatch):
    archive = tmp_path / "two files.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("first.txt", b"first completed payload")
        handle.writestr("second.txt", b"second completed payload")
    root = tmp_path / "output"
    root.mkdir()
    first, second = root / "first.txt", root / "second.txt"
    first.write_bytes(OLD)
    second.write_bytes(OLD)
    replace = core.os.replace

    def refuse_second_commit(source, final, *args, **kwargs):
        if Path(final) == second:
            raise PermissionError("second destination is locked")
        return replace(source, final, *args, **kwargs)

    monkeypatch.setattr(core.os, "replace", refuse_second_commit)
    with pytest.raises(core.ArchiveError):
        engine.extract(archive, root)
    # The guarantee is per-file; committed earlier files may remain.
    assert first.read_bytes() == b"first completed payload"
    assert second.read_bytes() == OLD
    assert_no_temporary_files(root)


def test_long_final_filename_does_not_make_temporary_basename_too_long(engine, tmp_path):
    name = "n" * 236 + ".txt"
    archive = independent_archive(tmp_path, "zip", b"long filename", name=name)
    root, target = destination(tmp_path, name=name)
    engine.extract(archive, root)
    assert target.read_bytes() == b"long filename"
    assert_no_temporary_files(root)


@pytest.mark.parametrize("suffix", ["zip", "tar"])
def test_no_overwrite_skips_existing_file_without_opening_payload(
    engine, tmp_path, monkeypatch, suffix,
):
    archive = independent_archive(tmp_path, suffix)
    root, target = destination(tmp_path)
    if suffix == "zip":
        open_member = zipfile.ZipFile.open

        def refuse_payload(handle, name, mode="r", *args, **kwargs):
            if mode == "r":
                pytest.fail("existing no-overwrite destination must not start ZIP payload extraction")
            return open_member(handle, name, mode, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "open", refuse_payload)
    else:
        monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda *args, **kwargs:
                            pytest.fail("existing no-overwrite destination must not start TAR payload extraction"))
    report = engine.extract(archive, root, overwrite=False)
    assert target.read_bytes() == OLD
    assert report.files == 0
    assert "payload.txt" in report.skipped
    assert_no_temporary_files(root)


@pytest.mark.parametrize("competitor", ["file", "directory", "symlink"])
def test_no_overwrite_commit_race_preserves_competing_entry(
    engine, tmp_path, monkeypatch, competitor,
):
    archive = independent_archive(tmp_path, "zip")
    root, target = destination(tmp_path, existing=False)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside unchanged")
    commit = core.SafeOutputFile.commit

    def race_at_commit(output):
        if competitor == "file":
            target.write_bytes(OLD)
        elif competitor == "directory":
            target.mkdir()
            (target / "keep.txt").write_bytes(OLD)
        else:
            try:
                target.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                pytest.skip(f"platform cannot create competing symlink: {exc}")
        return commit(output)

    monkeypatch.setattr(core.SafeOutputFile, "commit", race_at_commit)
    report = engine.extract(archive, root, overwrite=False)
    assert report.files == 0
    assert "payload.txt" in report.skipped
    if competitor == "directory":
        assert (target / "keep.txt").read_bytes() == OLD
    elif competitor == "symlink":
        assert target.is_symlink()
        assert outside.read_bytes() == b"outside unchanged"
    else:
        assert target.read_bytes() == OLD
    assert_no_temporary_files(root)


def test_no_overwrite_commit_fails_closed_without_hardlink_support(engine, tmp_path, monkeypatch):
    archive = independent_archive(tmp_path, "zip")
    root, target = destination(tmp_path, existing=False)

    def unsupported_link(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "filesystem does not support atomic hard links")

    monkeypatch.setattr(core.os, "link", unsupported_link)
    with pytest.raises(core.ArchiveError):
        engine.extract(archive, root, overwrite=False)
    assert not target.exists()
    assert_no_temporary_files(root)


def test_regular_payload_never_removes_existing_directory(engine, tmp_path):
    archive = independent_archive(tmp_path, "zip")
    root, target = destination(tmp_path, existing=False)
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_bytes(OLD)
    with pytest.raises(core.ArchiveError):
        engine.extract(archive, root)
    assert target.is_dir()
    assert marker.read_bytes() == OLD
    assert_no_temporary_files(root)


def test_no_overwrite_preserves_existing_directory(engine, tmp_path):
    archive = independent_archive(tmp_path, "zip")
    root, target = destination(tmp_path, existing=False)
    target.mkdir()
    (target / "keep.txt").write_bytes(OLD)
    report = engine.extract(archive, root, overwrite=False)
    assert report.files == 0
    assert "payload.txt" in report.skipped
    assert (target / "keep.txt").read_bytes() == OLD
    assert_no_temporary_files(root)
