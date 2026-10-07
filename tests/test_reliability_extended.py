"""Optional decoders share transactional outputs, including late CRC failures."""
from contextlib import contextmanager
import os
import stat

import pytest

import snug
import snug_core as core
from snug_ext import LibarchiveBackend


PAYLOAD = b"transactional CRC payload: never expose partial content\n"


def destination(tmp_path, existing):
    dest = tmp_path / "output"
    dest.mkdir()
    target = dest / "payload.txt"
    if existing:
        target.write_bytes(b"previous working file")
        target.chmod(0o600)
    return dest, target


def assert_clean(dest, target, existing):
    assert not list(dest.rglob(".snug-part-*"))
    if existing:
        assert target.read_bytes() == b"previous working file"
        if os.name != "nt":
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
    else:
        assert not target.exists()


def seven_archive(py7zr_backend, tmp_path, password=None):
    archive = tmp_path / "sample.7z"
    filters = [{"id": py7zr_backend.FILTER_COPY}]
    if password:
        filters.append({"id": py7zr_backend.FILTER_CRYPTO_AES256_SHA256})
    with py7zr_backend.SevenZipFile(archive, "w", filters=filters, password=password) as handle:
        handle.writestr(PAYLOAD, "payload.txt")
    return archive


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["crc", "wrong-password", "write-error", "interrupt", "close-error"])
def test_sevenzip_failure_keeps_old_file_and_removes_staging(
    engine, tmp_path, py7zr_backend, monkeypatch, failure, existing,
):
    archive = seven_archive(py7zr_backend, tmp_path, "correct password" if failure == "wrong-password" else None)
    dest, target = destination(tmp_path, existing)
    password = "wrong password" if failure == "wrong-password" else None
    if failure == "crc":
        raw = archive.read_bytes()
        assert PAYLOAD in raw  # COPY data lets this fixture target the payload, not headers.
        archive.write_bytes(raw.replace(PAYLOAD, b"X" + PAYLOAD[1:], 1))
    elif failure in ("write-error", "interrupt"):
        write = core.SafeOutputFile.write

        def failed_write(self, data):
            write(self, data[:7])
            if failure == "interrupt":
                raise KeyboardInterrupt()
            raise OSError("simulated output write failure")

        monkeypatch.setattr(core.SafeOutputFile, "write", failed_write)
    elif failure == "close-error":
        close = py7zr_backend.SevenZipFile.close

        def failed_close(self):
            close(self)
            if self.mode == "r":
                raise snug.ArchiveError("simulated archive-close validation failure")

        monkeypatch.setattr(py7zr_backend.SevenZipFile, "close", failed_close)
    expected = KeyboardInterrupt if failure == "interrupt" else snug.ArchiveError
    with pytest.raises(expected):
        engine.extract(archive, dest, password=password)
    assert_clean(dest, target, existing)


def test_sevenzip_existing_file_is_still_visible_until_archive_closes(
    engine, tmp_path, py7zr_backend, monkeypatch,
):
    archive = seven_archive(py7zr_backend, tmp_path)
    dest, target = destination(tmp_path, True)
    close = py7zr_backend.SevenZipFile.close
    observations = []

    def checked_close(self):
        if self.mode == "r":
            observations.append(target.read_bytes())
            assert list(dest.rglob(".snug-part-*"))
        close(self)

    monkeypatch.setattr(py7zr_backend.SevenZipFile, "close", checked_close)
    report = engine.extract(archive, dest)
    assert observations == [b"previous working file"]
    assert target.read_bytes() == PAYLOAD
    assert report.files == 1 and report.bytes_written == len(PAYLOAD)
    assert not list(dest.rglob(".snug-part-*"))


def test_sevenzip_no_overwrite_skips_existing_payload(engine, tmp_path, py7zr_backend, monkeypatch):
    archive = seven_archive(py7zr_backend, tmp_path)
    dest, target = destination(tmp_path, True)
    monkeypatch.setattr(py7zr_backend.SevenZipFile, "extract", lambda *args, **kwargs: pytest.fail("existing payload should not be extracted"))
    report = engine.extract(archive, dest, overwrite=False)
    assert report.files == 0 and report.skipped == ["payload.txt"]
    assert_clean(dest, target, True)


def test_sevenzip_no_overwrite_rechecks_at_atomic_commit(engine, tmp_path, py7zr_backend, monkeypatch):
    archive = seven_archive(py7zr_backend, tmp_path)
    dest, target = destination(tmp_path, False)
    commit = core.SafeOutputFile.commit

    def racing_commit(self):
        if self.target == target:
            target.write_bytes(b"concurrent writer wins")
        commit(self)

    monkeypatch.setattr(core.SafeOutputFile, "commit", racing_commit)
    report = engine.extract(archive, dest, overwrite=False)
    assert target.read_bytes() == b"concurrent writer wins"
    assert report.files == 0 and report.skipped == ["payload.txt"]
    assert not list(dest.rglob(".snug-part-*"))


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["stream-error", "interrupt", "close-error"])
def test_libarchive_failure_keeps_old_file_and_removes_staging(
    engine, tmp_path, libarchive_backend, monkeypatch, failure, existing,
):
    source = tmp_path / "payload.txt"
    source.write_bytes(PAYLOAD)
    archive = tmp_path / "sample.cpio"
    engine.create(archive, [source])
    dest, target = destination(tmp_path, existing)
    if failure == "close-error":
        file_reader = libarchive_backend.file_reader

        @contextmanager
        def failed_reader(*args, **kwargs):
            with file_reader(*args, **kwargs) as reader:
                yield reader
            if list(dest.rglob(".snug-part-*")):
                raise OSError("simulated libarchive reader-close failure")

        monkeypatch.setattr(libarchive_backend, "file_reader", failed_reader)
    else:
        chunks = LibarchiveBackend._entry_chunks

        def failed_chunks(raw, size):
            for block in chunks(raw, size):
                yield block[:7]
                if failure == "interrupt":
                    raise KeyboardInterrupt()
                raise snug.ArchiveError("simulated corrupt decoder stream")

        monkeypatch.setattr(LibarchiveBackend, "_entry_chunks", staticmethod(failed_chunks))
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else snug.ArchiveError):
        engine.extract(archive, dest)
    assert_clean(dest, target, existing)
