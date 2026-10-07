"""Inspection returns headers and metadata together and is reused by the engine."""

import gzip
import io
from pathlib import Path
import tarfile
import zipfile

import pytest

import snug_core as core
from snug_ext import LibarchiveBackend


def archive_fixture(tmp_path, suffix):
    path = tmp_path / f"sample.{suffix}"
    if suffix == "zip":
        with zipfile.ZipFile(path, "w") as handle:
            handle.writestr("payload.txt", b"data")
    elif suffix == "tar":
        member = tarfile.TarInfo("payload.txt")
        member.size = 4
        with tarfile.open(path, "w") as handle:
            handle.addfile(member, io.BytesIO(b"data"))
    else:
        path.write_bytes(gzip.compress(b"data"))
    return path


@pytest.mark.parametrize("suffix", ["zip", "tar", "gz"])
def test_engine_inspection_preserves_legacy_entry_and_metadata_results(tmp_path, suffix):
    path = archive_fixture(tmp_path, suffix)
    backend = core.StreamBackend() if suffix == "gz" else core.NativeBackend()
    engine = core.ArchiveEngine([backend])
    inspection = engine.inspect(path)
    assert isinstance(inspection, core.ArchiveInspection)
    assert inspection.entries == backend.list_entries(path)
    assert inspection.metadata == backend.metadata(path)
    assert inspection.entries == engine.list_entries(path)
    assert len(inspection.entries) == 1
    assert inspection.entries[0].size == 4
    assert inspection.metadata["encrypted"] is False


class InspectionOnlyBackend:
    name = "inspection-only decoder"
    read_formats = {core.ArchiveFormat.ZIP}
    write_formats = {core.ArchiveFormat.ZIP}

    def __init__(self):
        self.calls = []
        self.received_inspection = None
        self.result = None

    def available(self):
        return True

    def can_read(self, path):
        return True

    def can_write(self, fmt):
        return fmt in self.write_formats

    def inspect(self, path, password=None):
        self.calls.append((Path(path), password))
        self.result = core.ArchiveInspection(
            [core.ArchiveEntry("payload.txt", size=4)],
            {"encrypted": False, "path": "untrusted override", "format": "untrusted format"},
        )
        return self.result

    def list_entries(self, *args, **kwargs):
        pytest.fail("engine must use combined inspection rather than a separate header pass")

    def metadata(self, *args, **kwargs):
        pytest.fail("engine must not run a separate metadata pass")

    def extract(self, path, ctx, password=None):
        self.received_inspection = ctx.inspection
        for entry in ctx.inspection.entries:
            core._extract_entry(ctx, entry, chunks=[b"data"])


@pytest.mark.parametrize("operation", ["inspect", "list", "info", "extract"])
def test_engine_calls_combined_inspection_once_and_forwards_password(tmp_path, operation):
    path = archive_fixture(tmp_path, "zip")
    backend = InspectionOnlyBackend()
    engine = core.ArchiveEngine([backend])
    if operation == "inspect":
        assert engine.inspect(path, password="test password") is backend.result
    elif operation == "list":
        assert len(engine.list_entries(path, password="test password")) == 1
    elif operation == "info":
        info = engine.info(path, password="test password")
        assert info["entries"] == 1 and info["files"] == 1
        assert info["uncompressed_size"] == 4
        assert info["path"] == str(path)
        assert info["format"] == "zip"
        assert info["encrypted"] is False
    else:
        report = engine.extract(path, tmp_path / "output", password="test password")
        assert report.files == 1
        assert (tmp_path / "output/payload.txt").read_bytes() == b"data"
        assert backend.received_inspection is backend.result
    assert backend.calls == [(path, "test password")]


@pytest.mark.parametrize("operation", ["info", "extract"])
def test_libarchive_inspection_is_not_repeated_for_metadata_or_extraction(
    tmp_path, libarchive_backend, monkeypatch, operation,
):
    path = tmp_path / "independent.cpio"
    with libarchive_backend.file_writer(str(path), "cpio_newc") as writer:
        writer.add_file_from_memory("payload.txt", 4, b"data")
    backend = LibarchiveBackend()
    inspect = backend.inspect
    calls = []

    def counted_inspect(archive, password=None):
        calls.append(Path(archive))
        return inspect(archive, password=password)

    monkeypatch.setattr(backend, "inspect", counted_inspect)
    engine = core.ArchiveEngine([backend])
    if operation == "info":
        info = engine.info(path)
        assert info["files"] == 1 and info["uncompressed_size"] == 4
    else:
        report = engine.extract(path, tmp_path / "output")
        assert report.files == 1
        assert (tmp_path / "output/payload.txt").read_bytes() == b"data"
    assert calls == [path]


def test_inspection_does_not_decode_zip_payload_or_claim_integrity(tmp_path):
    path = archive_fixture(tmp_path, "zip")
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("payload.txt")
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    damaged = bytearray(path.read_bytes())
    damaged[offset] ^= 0xFF
    path.write_bytes(damaged)
    inspection = core.ArchiveEngine([core.NativeBackend()]).inspect(path)
    assert inspection.entries[0].name == "payload.txt"
    assert inspection.entries[0].size == 4
