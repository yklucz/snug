"""Explicit extraction quotas constrain headers and streamed decoder output."""

import bz2
from dataclasses import FrozenInstanceError
import gzip
import io
import lzma
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

import snug_core as core


FORMATS = ["zip", "tar", "tar.gz", "gz", "bz2", "xz", "lzma", "7z", "cpio"]
PAYLOAD = b"explicit extraction budget\n" * 256
OLD = b"keep the existing destination"


@pytest.mark.parametrize("text,expected", [
    ("0", 0), ("17", 17), ("17B", 17), ("1K", 1000), ("500KB", 500000),
    ("500M", 500000000), ("2G", 2000000000), ("20GB", 20000000000),
    ("1TB", 1000000000000), ("1KiB", 1024), ("1MiB", 1048576),
    ("1GiB", 1073741824), ("1TiB", 1099511627776),
    ("1.5KiB", 1536), ("0.001KB", 1), ("0.000001MB", 1),
])
def test_size_units_resolve_exact_bytes(text, expected):
    assert core.parse_size(text) == expected


@pytest.mark.parametrize("text", [
    "", " ", "-1", "+1", "1e6", "NaN", "inf", "1m", "1kb", "1KIB", "1Gi",
    "1MiBB", "1MBps", "1 GB", " 1GB", "1GB ", "1/2GB", "1.5", "0.1KiB",
    "1.0000000000000000000000000000000000000001B", "1" * 129,
])
def test_malformed_ambiguous_or_fractional_byte_sizes_fail(text):
    with pytest.raises(ValueError):
        core.parse_size(text)


def test_limits_are_frozen_and_have_no_restrictive_defaults():
    limits = core.ExtractionLimits()
    assert limits.max_entries is None
    assert limits.max_total_size is None
    assert limits.max_file_size is None
    assert limits.max_ratio is None
    with pytest.raises(FrozenInstanceError):
        limits.max_entries = 1
    assert issubclass(core.ResourceLimitError, core.ArchiveError)


@pytest.mark.parametrize("values", [
    {"max_entries": -1}, {"max_entries": 1.5}, {"max_entries": True},
    {"max_total_size": -1}, {"max_file_size": -1}, {"max_file_size": 0.5},
    {"max_ratio": 0}, {"max_ratio": -1}, {"max_ratio": float("nan")},
    {"max_ratio": float("inf")}, {"max_ratio": True},
])
def test_invalid_limits_are_rejected_before_extraction(values):
    with pytest.raises(ValueError):
        core.ExtractionLimits(**values)


def archive_fixture(tmp_path, suffix, request, content=PAYLOAD, *, compressed=False):
    path = tmp_path / (f"payload.txt.{suffix}" if suffix in {"gz", "bz2", "xz", "lzma"}
                       else f"sample.{suffix}")
    if suffix == "zip":
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED if compressed
                             else zipfile.ZIP_STORED) as handle:
            handle.writestr("payload.txt", content)
    elif suffix in {"tar", "tar.gz"}:
        member = tarfile.TarInfo("payload.txt")
        member.size = len(content)
        with tarfile.open(path, "w:gz" if suffix == "tar.gz" else "w") as handle:
            handle.addfile(member, io.BytesIO(content))
    elif suffix == "7z":
        library = request.getfixturevalue("py7zr_backend")
        with library.SevenZipFile(path, "w") as handle:
            handle.writestr(content, "payload.txt")
    elif suffix == "cpio":
        library = request.getfixturevalue("libarchive_backend")
        with library.file_writer(str(path), "cpio_newc") as handle:
            handle.add_file_from_memory("payload.txt", len(content), content)
    else:
        codec = {"gz": gzip, "bz2": bz2, "xz": lzma, "lzma": lzma}[suffix]
        options = {"format": lzma.FORMAT_ALONE} if suffix == "lzma" else {}
        path.write_bytes(codec.compress(content, **options))
    return path


def existing_destination(tmp_path, names=("payload.txt",)):
    root = tmp_path / "output"
    root.mkdir()
    for name in names:
        (root / name).write_bytes(OLD)
    return root


def assert_preserved(root, names=("payload.txt",)):
    assert sorted(path.name for path in root.iterdir()) == sorted(names)
    for name in names:
        assert (root / name).read_bytes() == OLD
    assert not list(root.rglob(".snug-part-*"))


@pytest.mark.parametrize("suffix", FORMATS)
@pytest.mark.parametrize("field", ["max_entries", "max_total_size", "max_file_size"])
def test_explicit_limits_apply_to_each_real_backend(engine, tmp_path, request, suffix, field):
    path = archive_fixture(tmp_path, suffix, request)
    root = existing_destination(tmp_path)
    maximum = 0 if field == "max_entries" else len(PAYLOAD) - 1
    with pytest.raises(core.ResourceLimitError):
        engine.extract(path, root, limits=core.ExtractionLimits(**{field: maximum}))
    assert_preserved(root)


@pytest.mark.parametrize("suffix", FORMATS)
def test_exact_byte_and_entry_boundaries_succeed(engine, tmp_path, request, suffix):
    path = archive_fixture(tmp_path, suffix, request)
    root = existing_destination(tmp_path)
    report = engine.extract(path, root, limits=core.ExtractionLimits(
        max_entries=1, max_total_size=len(PAYLOAD), max_file_size=len(PAYLOAD)))
    assert report.files == 1
    assert report.bytes_written == len(PAYLOAD)
    assert (root / "payload.txt").read_bytes() == PAYLOAD
    assert not list(root.rglob(".snug-part-*"))


@pytest.mark.parametrize("suffix", FORMATS)
def test_zero_byte_member_accepts_zero_byte_budget(engine, tmp_path, request, suffix):
    path = archive_fixture(tmp_path, suffix, request, content=b"")
    root = existing_destination(tmp_path)
    report = engine.extract(path, root, limits=core.ExtractionLimits(
        max_entries=1, max_total_size=0, max_file_size=0, max_ratio=1))
    assert report.files == 1 and report.bytes_written == 0
    assert (root / "payload.txt").read_bytes() == b""
    assert not list(root.rglob(".snug-part-*"))


def test_selected_entry_preflight_counts_directories_and_stops_before_mutation(engine, tmp_path):
    path = tmp_path / "selected.zip"
    with zipfile.ZipFile(path, "w") as handle:
        handle.writestr("nested/", b"")
        handle.writestr("first.txt", b"abc")
        handle.writestr("second.txt", b"def")
    root = tmp_path / "new destination"
    with pytest.raises(core.ResourceLimitError, match="3 selected entries; limit is 2"):
        engine.extract(path, root, limits=core.ExtractionLimits(max_entries=2))
    assert not root.exists()


@pytest.mark.parametrize("selection", [("first.txt",), lambda name: name == "first.txt"])
def test_limits_count_only_selected_members(engine, tmp_path, selection):
    path = tmp_path / "selected.zip"
    with zipfile.ZipFile(path, "w") as handle:
        handle.writestr("first.txt", b"abc")
        handle.writestr("second.txt", b"huge unselected payload" * 1000)
    root = tmp_path / "output"
    report = engine.extract(path, root, members=selection, limits=core.ExtractionLimits(
        max_entries=1, max_total_size=3, max_file_size=3))
    assert report.files == 1
    assert (root / "first.txt").read_bytes() == b"abc"
    assert not (root / "second.txt").exists()


class SyntheticBackend:
    """A decoder can lie about declared sizes; budgets must use its real chunks."""

    name = "synthetic decoder"
    read_formats = {core.ArchiveFormat.ZIP}
    write_formats = set()

    def __init__(self, entries, payloads):
        self.entries, self.payloads = entries, payloads
        self.streamed = []
        self.last_ctx = None

    def available(self):
        return True

    def can_read(self, path):
        return True

    def can_write(self, fmt):
        return False

    def list_entries(self, path, password=None):
        return list(self.entries)

    def metadata(self, path, password=None):
        return {}

    def inspect(self, path, password=None):
        return core.ArchiveInspection(entries=list(self.entries), metadata={})

    def extract(self, path, ctx, password=None):
        self.last_ctx = ctx
        for entry in self.entries:
            if core._selected(entry.name, ctx.members):
                core._extract_entry(ctx, entry, chunks=self.chunks(entry))

    def chunks(self, entry):
        for chunk in self.payloads[entry.name]:
            self.streamed.append((entry.name, len(chunk)))
            yield chunk


def synthetic_engine(tmp_path, entries, payloads):
    path = tmp_path / "decoder.zip"
    with zipfile.ZipFile(path, "w") as handle:
        handle.writestr("unused", b"format detection only")
    backend = SyntheticBackend(entries, payloads)
    return core.ArchiveEngine([backend]), backend, path


@pytest.mark.parametrize("field", ["max_total_size", "max_file_size"])
def test_lying_declared_size_cannot_bypass_actual_byte_limits(tmp_path, field):
    entry = core.ArchiveEntry("payload.txt", size=1)
    engine, backend, path = synthetic_engine(tmp_path, [entry], {entry.name: [b"12", b"34", b"56"]})
    root = existing_destination(tmp_path)
    with pytest.raises(core.ResourceLimitError):
        engine.extract(path, root, limits=core.ExtractionLimits(**{field: 3}))
    assert backend.streamed == [(entry.name, 2), (entry.name, 2)]
    assert backend.last_ctx.report.bytes_written == 2
    assert_preserved(root)


def test_actual_total_budget_accumulates_across_members(tmp_path):
    entries = [core.ArchiveEntry(name, size_known=False) for name in ("first.txt", "second.txt")]
    engine, backend, path = synthetic_engine(tmp_path, entries, {
        "first.txt": [b"12"], "second.txt": [b"34"],
    })
    root = existing_destination(tmp_path, names=("first.txt", "second.txt"))
    with pytest.raises(core.ResourceLimitError, match="total limit"):
        engine.extract(path, root, limits=core.ExtractionLimits(max_total_size=3))
    assert backend.last_ctx.report.bytes_written == 2
    assert_preserved(root, names=("first.txt", "second.txt"))


@pytest.mark.parametrize("field", ["max_total_size", "max_file_size"])
def test_declared_size_limit_fails_before_decoder_is_started(tmp_path, field):
    entry = core.ArchiveEntry("payload.txt", size=10)
    engine, backend, path = synthetic_engine(tmp_path, [entry], {entry.name: [b"1234567890"]})
    root = tmp_path / "output"
    with pytest.raises(core.ResourceLimitError):
        engine.extract(path, root, limits=core.ExtractionLimits(**{field: 9}))
    assert backend.streamed == []
    assert backend.last_ctx is None
    assert not root.exists()


def test_missing_compressed_size_skips_ratio_but_keeps_byte_enforcement(tmp_path):
    entry = core.ArchiveEntry("payload.txt", size_known=False, compressed_size=None)
    engine, _, path = synthetic_engine(tmp_path, [entry], {entry.name: [b"12345678"]})
    root = existing_destination(tmp_path)
    report = engine.extract(path, root, limits=core.ExtractionLimits(
        max_file_size=8, max_total_size=8, max_ratio=0.01))
    assert report.bytes_written == 8
    assert (root / entry.name).read_bytes() == b"12345678"
    (root / entry.name).write_bytes(OLD)
    with pytest.raises(core.ResourceLimitError, match="file limit"):
        engine.extract(path, root, limits=core.ExtractionLimits(max_file_size=7, max_ratio=0.01))
    assert_preserved(root)


def test_known_compressed_ratio_is_checked_before_payload_write(engine, tmp_path, request):
    path = archive_fixture(tmp_path, "zip", request, compressed=True)
    root = existing_destination(tmp_path)
    with pytest.raises(core.ResourceLimitError, match="ratio"):
        engine.extract(path, root, limits=core.ExtractionLimits(max_ratio=2))
    assert_preserved(root)


def test_actual_ratio_overflow_is_not_hidden_by_small_declared_size(tmp_path):
    entry = core.ArchiveEntry("payload.txt", size=1, compressed_size=1)
    engine, backend, path = synthetic_engine(tmp_path, [entry], {entry.name: [b"12", b"34"]})
    root = existing_destination(tmp_path)
    with pytest.raises(core.ResourceLimitError, match="ratio"):
        engine.extract(path, root, limits=core.ExtractionLimits(max_ratio=2))
    assert backend.last_ctx.report.bytes_written == 2
    assert_preserved(root)


@pytest.mark.parametrize("size", [0, 1])
def test_zero_compressed_size_has_defined_ratio_behavior(tmp_path, size):
    entry = core.ArchiveEntry("payload.txt", size=size, compressed_size=0)
    engine, _, path = synthetic_engine(tmp_path, [entry], {entry.name: [b"x"] if size else []})
    root = existing_destination(tmp_path)
    if size:
        with pytest.raises(core.ResourceLimitError, match="ratio"):
            engine.extract(path, root, limits=core.ExtractionLimits(max_ratio=1))
        assert_preserved(root)
    else:
        report = engine.extract(path, root, limits=core.ExtractionLimits(max_ratio=1))
        assert report.bytes_written == 0
        assert (root / entry.name).read_bytes() == b""


@pytest.mark.parametrize("suffix", ["gz", "bz2", "xz", "lzma"])
def test_stream_limits_do_not_require_a_full_preflight_decode(engine, tmp_path, request, monkeypatch, suffix):
    path = archive_fixture(tmp_path, suffix, request)
    root = existing_destination(tmp_path)
    monkeypatch.setattr(core.StreamBackend, "list_entries", lambda *args, **kwargs:
                        pytest.fail("limit preflight must not fully decode an unknown-size stream"))
    with pytest.raises(core.ResourceLimitError):
        engine.extract(path, root, limits=core.ExtractionLimits(max_file_size=8))
    assert_preserved(root)


@pytest.mark.parametrize("flag,value", [
    ("--max-files", "0"), ("--max-size", "1K"), ("--max-file-size", "1KiB"),
    ("--max-ratio", "2"),
])
def test_cli_limit_failure_uses_exit_four_and_preserves_destination(tmp_path, request, flag, value):
    path = archive_fixture(tmp_path, "zip", request, compressed=True)
    root = existing_destination(tmp_path)
    env = dict(os.environ, SNUG_NO_UPDATE_CHECK="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, str(Path(core.__file__).with_name("snug.py")),
                             "extract", str(path), "-C", str(root), flag, value, "-q"],
                            cwd=tmp_path, env=env, capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "error:" in result.stderr
    assert "Traceback" not in result.stderr
    assert_preserved(root)


@pytest.mark.parametrize("args", [
    ["--max-si", "1GB"], ["--max-files", "-1"], ["--max-size", "1GiBB"],
    ["--max-ratio", "NaN"], ["--max-ratio", "0"],
])
def test_invalid_or_abbreviated_limit_flags_fail_cleanly(tmp_path, request, args):
    path = archive_fixture(tmp_path, "zip", request)
    root = existing_destination(tmp_path)
    env = dict(os.environ, SNUG_NO_UPDATE_CHECK="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, str(Path(core.__file__).with_name("snug.py")),
                             "extract", str(path), "-C", str(root), *args],
                            cwd=tmp_path, env=env, capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert_preserved(root)
