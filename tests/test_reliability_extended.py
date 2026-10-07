"""Optional decoders share transactional outputs, including late CRC failures."""
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import stat

import pytest

import snug
import snug_core as core
from snug_ext import LibarchiveBackend, SevenZipBackend


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
            if self.mode == "r" and list(dest.rglob(".snug-part-*")):
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
        if self.mode == "r" and list(dest.rglob(".snug-part-*")):
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

        def failed_chunks(raw, size, **kwargs):
            for block in chunks(raw, size, **kwargs):
                yield block[:7]
                if failure == "interrupt":
                    raise KeyboardInterrupt()
                raise snug.ArchiveError("simulated corrupt decoder stream")

        monkeypatch.setattr(LibarchiveBackend, "_entry_chunks", staticmethod(failed_chunks))
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else snug.ArchiveError):
        engine.extract(archive, dest)
    assert_clean(dest, target, existing)


@pytest.mark.parametrize("suffix,backend", [("7z", "py7zr_backend"), ("cpio", "libarchive_backend")])
@pytest.mark.parametrize("limit", ["max_entries", "max_total_size", "max_file_size"])
def test_optional_declared_limits_preserve_destination(engine, tmp_path, request, suffix, backend, limit):
    request.getfixturevalue(backend)
    sources = [tmp_path / "payload.txt", tmp_path / "second.txt"]
    for source in sources:
        source.write_bytes(PAYLOAD)
    archive = tmp_path / f"sample.{suffix}"
    engine.create(archive, sources)
    dest, target = destination(tmp_path, True)
    amount = {"max_entries": 1, "max_total_size": len(PAYLOAD) * 2 - 1,
              "max_file_size": len(PAYLOAD) - 1}[limit]
    with pytest.raises(core.ResourceLimitError):
        engine.extract(archive, dest, limits=core.ExtractionLimits(**{limit: amount}))
    assert_clean(dest, target, True)
    assert not (dest / "second.txt").exists()


@pytest.mark.parametrize("suffix,backend", [("7z", "py7zr_backend"), ("cpio", "libarchive_backend")])
@pytest.mark.parametrize("size_known", [False, True])
def test_optional_actual_byte_limits_override_missing_or_lying_metadata(
    engine, tmp_path, monkeypatch, request, suffix, backend, size_known,
):
    request.getfixturevalue(backend)
    source = tmp_path / "payload.txt"
    source.write_bytes(PAYLOAD)
    archive = tmp_path / f"sample.{suffix}"
    engine.create(archive, [source])
    dest, target = destination(tmp_path, True)
    if suffix == "cpio":
        entry = LibarchiveBackend._entry
        monkeypatch.setattr(LibarchiveBackend, "_entry", staticmethod(
            lambda raw: replace(entry(raw), size=1, size_known=size_known)))
    else:
        entries = SevenZipBackend._entries
        monkeypatch.setattr(SevenZipBackend, "_entries", staticmethod(
            lambda handle: [replace(item, size=1, size_known=size_known) for item in entries(handle)]))
    with pytest.raises(core.ResourceLimitError, match="file limit"):
        engine.extract(archive, dest, limits=core.ExtractionLimits(max_file_size=len(PAYLOAD) - 1))
    assert_clean(dest, target, True)


def test_sevenzip_non_solid_ratio_is_enforced(engine, tmp_path, py7zr_backend):
    source = tmp_path / "payload.txt"
    source.write_bytes(b"a" * 10000)
    archive = tmp_path / "sample.7z"
    engine.create(archive, [source])
    entries = engine.list_entries(archive)
    assert len(entries) == 1 and entries[0].compressed_size is not None
    with pytest.raises(core.ResourceLimitError, match="ratio"):
        engine.extract(archive, tmp_path / "out", limits=core.ExtractionLimits(max_ratio=2))


def test_solid_sevenzip_has_no_invented_per_file_ratio(engine, tmp_path, py7zr_backend):
    sources = [tmp_path / "one.txt", tmp_path / "two.txt"]
    for source in sources:
        source.write_bytes(b"a" * 1000)
    archive = tmp_path / "solid.7z"
    engine.create(archive, sources)
    entries = engine.list_entries(archive)
    assert len(entries) == 2 and all(entry.compressed_size is None for entry in entries)
    report = engine.extract(archive, tmp_path / "out", limits=core.ExtractionLimits(max_ratio=1, max_total_size=2000))
    assert report.files == 2 and report.bytes_written == 2000


def test_libarchive_missing_compressed_sizes_skip_ratio_but_keep_byte_limits(engine, tmp_path, libarchive_backend):
    source = tmp_path / "payload.txt"
    source.write_bytes(PAYLOAD)
    archive = tmp_path / "sample.cpio"
    engine.create(archive, [source])
    assert engine.list_entries(archive)[0].compressed_size is None
    report = engine.extract(archive, tmp_path / "out", limits=core.ExtractionLimits(max_ratio=1, max_file_size=len(PAYLOAD)))
    assert report.files == 1 and report.bytes_written == len(PAYLOAD)


def test_sevenzip_symlink_payload_obeys_actual_budget(engine, tmp_path, py7zr_backend, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.txt").write_bytes(b"target")
    try:
        (source / "link").symlink_to("target.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    archive = tmp_path / "link.7z"
    engine.create(archive, [source], root=source)
    entries = SevenZipBackend._entries
    monkeypatch.setattr(SevenZipBackend, "_entries", staticmethod(
        lambda handle: [replace(item, size=0, size_known=False) if item.is_symlink else item
                        for item in entries(handle)]))
    dest = tmp_path / "out"
    with pytest.raises(core.ResourceLimitError, match="file limit"):
        engine.extract(archive, dest, members=["link"], limits=core.ExtractionLimits(max_file_size=3))
    assert not (dest / "link").exists() and not (dest / "link").is_symlink()
    assert not list(dest.rglob(".snug-part-*"))


@pytest.mark.parametrize("suffix,backend", [("7z", "py7zr_backend"), ("cpio", "libarchive_backend")])
def test_optional_empty_file_obeys_zero_byte_limits(engine, tmp_path, request, suffix, backend):
    request.getfixturevalue(backend)
    source = tmp_path / "empty.txt"
    source.touch()
    archive = tmp_path / f"empty.{suffix}"
    engine.create(archive, [source])
    report = engine.extract(archive, tmp_path / "out", limits=core.ExtractionLimits(
        max_entries=1, max_total_size=0, max_file_size=0, max_ratio=1))
    assert report.files == 1 and report.bytes_written == 0
    assert (tmp_path / "out/empty.txt").read_bytes() == b""


@pytest.mark.parametrize("encrypted", [False, True])
def test_sevenzip_inspection_has_entries_and_metadata_in_one_open(tmp_path, py7zr_backend, monkeypatch, encrypted):
    password = "inspection password" if encrypted else None
    archive = seven_archive(py7zr_backend, tmp_path, password)
    initialize = py7zr_backend.SevenZipFile.__init__
    opens = []

    def counted_open(self, *args, **kwargs):
        opens.append(args[1] if len(args) > 1 else kwargs.get("mode", "r"))
        initialize(self, *args, **kwargs)

    monkeypatch.setattr(py7zr_backend.SevenZipFile, "__init__", counted_open)
    inspection = SevenZipBackend().inspect(archive, password)
    assert isinstance(inspection, core.ArchiveInspection)
    assert [entry.name for entry in inspection.entries] == ["payload.txt"]
    assert inspection.metadata == {"format": "7z", "encrypted": encrypted}
    assert opens == ["r"]


@pytest.mark.parametrize("backend_type", [SevenZipBackend, LibarchiveBackend])
def test_optional_legacy_inspection_wrappers_remain_compatible(monkeypatch, tmp_path, backend_type):
    backend = backend_type()
    inspection = core.ArchiveInspection([core.ArchiveEntry("payload.txt", size=10)], {"format": "fixture"})
    calls = []

    def inspect(path, password=None):
        calls.append((path, password))
        return inspection

    monkeypatch.setattr(backend, "inspect", inspect)
    path = tmp_path / "unused.archive"
    assert backend.list_entries(path, "password") == inspection.entries
    assert backend.metadata(path, "password") == inspection.metadata
    assert calls == [(path, "password"), (path, "password")]


def test_libarchive_inspection_marks_special_entries_without_extracting_them(engine, tmp_path, libarchive_backend):
    archive = tmp_path / "pipe.cpio"
    with libarchive_backend.file_writer(str(archive), "cpio_newc") as writer:
        writer.add_file_from_memory("pipe", 0, b"", filetype=stat.S_IFIFO)
    inspection = LibarchiveBackend().inspect(archive)
    assert len(inspection.entries) == 1 and inspection.entries[0].is_special
    output = tmp_path / "out"
    report = engine.extract(archive, output)
    assert report.files == 0 and report.skipped == ["pipe"]
    assert not (output / "pipe").exists()


class IntegrityProgress:
    def __init__(self):
        self.starts = []
        self.items = []
        self.bytes = 0
        self.completed = False

    def start(self, total_bytes, total_items):
        self.starts.append((total_bytes, total_items))

    def item(self, name):
        self.items.append(name)

    def chunk(self, size):
        self.bytes += size

    def done(self):
        self.completed = True


def forbid_extraction_writes(monkeypatch):
    open_path = Path.open

    def read_only(self, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in ("w", "a", "x", "+")), f"integrity test attempted to write {self}"
        return open_path(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", read_only)
    monkeypatch.setattr(core, "SafeOutputFile", lambda *args: pytest.fail("integrity tests must not stage disk outputs"))


@pytest.mark.parametrize("encrypted", [False, True])
def test_sevenzip_integrity_consumes_payload_without_writing(engine, tmp_path, py7zr_backend, monkeypatch, encrypted):
    password = "correct password" if encrypted else None
    archive = seven_archive(py7zr_backend, tmp_path, password)
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    progress = IntegrityProgress()
    forbid_extraction_writes(monkeypatch)
    report = engine.test(archive, password=password, progress=progress)
    assert report.entries == 1 and report.files == 1 and report.bytes_read == len(PAYLOAD)
    assert progress.starts == [(len(PAYLOAD), 1)] and progress.bytes == len(PAYLOAD)
    assert progress.items == ["payload.txt"] and progress.completed
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("fixture", [
    "test_read_format_rar.rar", "test_read_format_rar5_compressed.rar", "test_read_format_cab_1.cab",
    "test_read_format_iso_2.iso", "test_read_format_zip_ppmd8.zipx", "test_read_format_lha_header0.lzh",
    "test_read_format_warc.warc", "test_read_format_cpio_svr4_gzip_rpm.rpm", "sample.xar",
])
def test_libarchive_fixture_integrity_reads_real_payload(engine, fixture_dir, libarchive_backend, monkeypatch, fixture):
    archive = fixture_dir / fixture
    inspection = LibarchiveBackend().inspect(archive)
    expected = sum(entry.size for entry in inspection.entries
                   if not (entry.is_dir or entry.is_symlink or entry.is_hardlink or entry.is_special))
    progress = IntegrityProgress()
    forbid_extraction_writes(monkeypatch)
    report = engine.test(archive, progress=progress)
    assert report.bytes_read == expected and progress.bytes == expected
    assert report.files > 0 and progress.completed and len(progress.starts) == 1


@pytest.mark.parametrize("failure", ["crc", "wrong-password", "close"])
def test_sevenzip_integrity_failure_names_member_without_disk_changes(
    engine, tmp_path, py7zr_backend, monkeypatch, failure,
):
    archive = seven_archive(py7zr_backend, tmp_path, "correct password" if failure == "wrong-password" else None)
    if failure == "crc":
        archive.write_bytes(archive.read_bytes().replace(PAYLOAD, b"X" + PAYLOAD[1:], 1))
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    progress = IntegrityProgress()
    if failure == "close":
        close = py7zr_backend.SevenZipFile.close

        def failed_close(self):
            close(self)
            if self.mode == "r" and progress.bytes:
                raise OSError("reader close validation failed")

        monkeypatch.setattr(py7zr_backend.SevenZipFile, "close", failed_close)
    forbid_extraction_writes(monkeypatch)
    with pytest.raises(core.ArchiveError, match="payload.txt"):
        engine.test(archive, password="wrong password" if failure == "wrong-password" else None, progress=progress)
    assert not progress.completed
    assert {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_sevenzip_truncated_integrity_returns_archive_error(engine, tmp_path, py7zr_backend, monkeypatch):
    archive = seven_archive(py7zr_backend, tmp_path)
    archive.write_bytes(archive.read_bytes()[:-12])
    forbid_extraction_writes(monkeypatch)
    with pytest.raises(core.ArchiveError):
        engine.test(archive)
    assert not list(tmp_path.rglob(".snug-part-*"))


@pytest.mark.parametrize("failure", ["stream", "close"])
def test_libarchive_integrity_failure_names_member(engine, tmp_path, libarchive_backend, monkeypatch, failure):
    source = tmp_path / "payload.txt"
    source.write_bytes(PAYLOAD)
    archive = tmp_path / "payload.cpio"
    engine.create(archive, [source])
    progress = IntegrityProgress()
    if failure == "stream":
        chunks = LibarchiveBackend._entry_chunks

        def failing_chunks(raw, size, **kwargs):
            for chunk in chunks(raw, size, **kwargs):
                yield chunk[:7]
                raise OSError("decoder rejected corrupted content")

        monkeypatch.setattr(LibarchiveBackend, "_entry_chunks", staticmethod(failing_chunks))
    else:
        reader = libarchive_backend.file_reader

        @contextmanager
        def failing_reader(*args, **kwargs):
            with reader(*args, **kwargs) as stream:
                yield stream
            if progress.bytes:
                raise OSError("reader close validation failed")

        monkeypatch.setattr(libarchive_backend, "file_reader", failing_reader)
    forbid_extraction_writes(monkeypatch)
    with pytest.raises(core.ArchiveError, match="payload.txt"):
        engine.test(archive, progress=progress)
    assert not progress.completed


@pytest.mark.parametrize("suffix,backend", [("7z", "py7zr_backend"), ("cpio", "libarchive_backend")])
def test_optional_integrity_with_unknown_sizes_uses_entry_progress(
    engine, tmp_path, request, monkeypatch, suffix, backend,
):
    request.getfixturevalue(backend)
    source = tmp_path / "payload.txt"
    source.write_bytes(PAYLOAD)
    archive = tmp_path / f"payload.{suffix}"
    engine.create(archive, [source])
    if suffix == "cpio":
        entry = LibarchiveBackend._entry
        monkeypatch.setattr(LibarchiveBackend, "_entry", staticmethod(
            lambda raw: replace(entry(raw), size=0, size_known=False)))
    else:
        entries = SevenZipBackend._entries
        monkeypatch.setattr(SevenZipBackend, "_entries", staticmethod(
            lambda archive: [replace(entry, size=0, size_known=False) for entry in entries(archive)]))
    progress = IntegrityProgress()
    report = engine.test(archive, progress=progress)
    assert progress.starts == [(0, 1)] and progress.items == ["payload.txt"]
    assert report.bytes_read == len(PAYLOAD) and progress.bytes == len(PAYLOAD)


def test_sevenzip_integrity_rejects_duplicate_paths_before_library_renaming(engine, tmp_path, py7zr_backend, monkeypatch):
    archive = tmp_path / "duplicate.7z"
    with py7zr_backend.SevenZipFile(archive, "w") as handle:
        handle._writestr(b"first", "same.txt")
        handle._writestr(b"second", "same.txt")
    monkeypatch.setattr(py7zr_backend.SevenZipFile, "extract", lambda *args, **kwargs: pytest.fail("duplicates must fail preflight"))
    with pytest.raises(core.UnsafeArchiveError, match="duplicate"):
        engine.test(archive)


def test_sevenzip_integrity_rejects_decoded_escaping_symlink(engine, tmp_path, py7zr_backend, monkeypatch):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep outside content")
    source = tmp_path / "source/link"
    source.parent.mkdir()
    try:
        source.symlink_to(Path("..") / outside.name)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    archive = tmp_path / "escaping.7z"
    with py7zr_backend.SevenZipFile(archive, "w") as handle:
        handle.write(source, "link")
    forbid_extraction_writes(monkeypatch)
    with pytest.raises(core.UnsafeArchiveError, match="escaping"):
        engine.test(archive)
    assert outside.read_bytes() == b"keep outside content"


@pytest.mark.parametrize("failure", ["unknown-writer", "missing-payload"])
def test_sevenzip_integrity_cannot_succeed_from_listing_only(engine, tmp_path, py7zr_backend, monkeypatch, failure):
    archive = seven_archive(py7zr_backend, tmp_path)

    def no_decode(self, *, targets, recursive, factory):
        if failure == "unknown-writer":
            factory.create("not-in-the-archive.txt")

    monkeypatch.setattr(py7zr_backend.SevenZipFile, "extract", no_decode)
    with pytest.raises(core.ArchiveError, match="unexpected|not decoded"):
        engine.test(archive)


def test_optional_integrity_password_errors_are_redacted(engine, tmp_path, libarchive_backend, monkeypatch):
    source = tmp_path / "payload.txt"
    source.write_bytes(PAYLOAD)
    archive = tmp_path / "payload.cpio"
    engine.create(archive, [source])
    password = "private password"

    def fail(*args, **kwargs):
        raise OSError(f"backend error involving {password}")
        yield b""  # A lazy decoder error, rather than an inspection error.

    monkeypatch.setattr(LibarchiveBackend, "_entry_chunks", staticmethod(fail))
    with pytest.raises(core.ArchiveError) as caught:
        engine.test(archive, password=password)
    assert password not in str(caught.value) and "[redacted]" in str(caught.value)
