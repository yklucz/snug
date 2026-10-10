"""TAR header truncation must fail in the shared inspection path."""

import bz2
import gzip
import io
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

import snug_core as core


SCRIPT = Path(__file__).resolve().parents[1] / "snug.py"
KINDS = ["ustar", "gnu-name", "gnu-link", "pax", "pax-global"]
CODECS = [("tar", None), ("tar.gz", gzip), ("tar.bz2", bz2), ("tar.xz", lzma)]


def writer_fixture(kind):
    """Return real writer bytes, expected logical members and their block ends."""
    fmt = {"ustar": tarfile.USTAR_FORMAT, "gnu-name": tarfile.GNU_FORMAT,
           "gnu-link": tarfile.GNU_FORMAT, "pax": tarfile.PAX_FORMAT,
           "pax-global": tarfile.PAX_FORMAT}[kind]
    second_name = ("long-" + "n" * 110 + ".txt" if kind == "gnu-name"
                   else "\u540d" * 40 + ".txt" if kind.startswith("pax") else "second.txt")
    members = [("first.txt", b"first", None),
               (second_name, b"second", None)]
    if kind == "gnu-link":
        members[1] = ("second-link", b"", "target-" + "n" * 110)
    output = io.BytesIO()
    options = {"pax_headers": {"comment": "global metadata"}} if kind == "pax-global" else {}
    with tarfile.open(fileobj=output, mode="w", format=fmt, **options) as archive:
        for name, payload, link in members:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            if link is not None:
                member.type = tarfile.SYMTYPE
                member.linkname = link
            archive.addfile(member, io.BytesIO(payload))
    data = output.getvalue()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        boundaries = [(member.offset_data + member.size + 511) // 512 * 512
                      for member in archive.getmembers()]
    # Remove only superfluous writer record padding, keeping both terminators.
    return data[:boundaries[-1] + 1024], members, boundaries


def physical_records(data):
    records = []
    cursor = 0
    while data[cursor:cursor + 512] != b"\0" * 512:
        member = tarfile.TarInfo.frombuf(data[cursor:cursor + 512], "utf-8", "surrogateescape")
        end = cursor + 512 + (member.size + 511) // 512 * 512
        records.append((cursor, member, end))
        cursor = end
    return records


def write_variant(path, data, codec):
    path.write_bytes(data if codec is None else codec.compress(data))


def verify_inspection(inspection, expected):
    assert [(entry.name, entry.size, entry.link_target) for entry in inspection.entries] == [
        (name, len(payload), link) for name, payload, link in expected
    ]


@pytest.fixture
def native_engine():
    return core.ArchiveEngine([core.NativeBackend()])


def pax_repro():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, payload in [("first.txt", b"first"), ("\u540d" * 40 + ".txt", b"second")]:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    data = output.getvalue()
    assert data[1024 + 156:1024 + 157] == tarfile.XHDTYPE
    return data[:1024 + 276]


@pytest.mark.parametrize("command", ["test", "list", "extract"])
def test_cli_rejects_original_partial_pax_header_before_destination(tmp_path, command):
    archive = tmp_path / "writer-cut-next-metadata-header-partial.tar"
    archive.write_bytes(pax_repro())
    destination = tmp_path / "new-destination"
    arguments = [command, str(archive)]
    if command == "extract":
        arguments += ["-C", str(destination)]
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments], cwd=tmp_path,
        env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1", PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    assert str(archive) in result.stderr
    assert "truncated" in result.stderr.lower()
    assert "Traceback" not in result.stderr
    assert "Archive is OK." not in result.stdout + result.stderr
    assert not destination.exists()


@pytest.mark.parametrize("kind", KINDS)
def test_every_plain_tar_cut_is_rejected_or_a_complete_archive_prefix(tmp_path, native_engine, kind):
    data, members, boundaries = writer_fixture(kind)
    archive = tmp_path / "every-cut.tar"
    accepted = set()
    for cut in range(len(data) + 1):
        archive.write_bytes(data[:cut])
        try:
            inspection = native_engine.inspect(archive)
        except core.ArchiveError:
            continue
        assert cut in boundaries or cut >= boundaries[-1], (kind, cut, inspection.entries)
        count = sum(boundary <= cut for boundary in boundaries)
        verify_inspection(inspection, members[:count])
        # Independently verify that accepted bytes contain each complete payload.
        with tarfile.open(archive, "r:") as reader:
            for name, payload, link in members[:count]:
                if link is None:
                    handle = reader.extractfile(name)
                    assert handle is not None
                    assert handle.read() == payload, (kind, cut, name)
        accepted.add(cut)
    assert set(boundaries) <= accepted
    assert len(data) in accepted


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("suffix,codec", CODECS)
@pytest.mark.parametrize("zero_blocks", [0, 1, 2, 5])
def test_complete_tar_prefix_accepts_missing_terminators_and_zero_padding(
    tmp_path, native_engine, kind, suffix, codec, zero_blocks,
):
    data, members, boundaries = writer_fixture(kind)
    archive = tmp_path / f"compatible.{suffix}"
    for count, boundary in enumerate(boundaries, start=1):
        write_variant(archive, data[:boundary] + b"\0" * (512 * zero_blocks), codec)
        verify_inspection(native_engine.inspect(archive), members[:count])
        report = native_engine.test(archive)
        assert report.entries == count
        assert report.bytes_read == sum(len(payload) for _, payload, link in members[:count]
                                        if link is None)
        destination = tmp_path / f"output-{count}"
        native_engine.extract(archive, destination)
        for name, payload, link in members[:count]:
            target = destination / name
            if link is None:
                assert target.read_bytes() == payload
            else:
                assert target.is_symlink()
                assert os.readlink(target) == link


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("suffix,codec", CODECS)
def test_sampled_header_and_extended_metadata_cuts_fail_before_destination(
    tmp_path, native_engine, kind, suffix, codec,
):
    data, _, boundaries = writer_fixture(kind)
    metadata_types = {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME,
                      tarfile.GNUTYPE_LONGLINK}
    cuts = set()
    for start, member, end in physical_records(data):
        cuts.update([start + 1, start + 156, start + 276, start + 511])
        if member.size:
            cuts.update([start + 512, start + 512 + member.size - 1])
            if member.size % 512:
                cuts.update([start + 512 + member.size, end - 1])
        if member.type in metadata_types:
            # A complete metadata record still requires a following real header.
            cuts.add(end)
    cuts -= set(boundaries)
    archive = tmp_path / f"sampled.{suffix}"
    for cut in sorted(cuts):
        write_variant(archive, data[:cut], codec)
        for operation in [native_engine.inspect, native_engine.list_entries, native_engine.test]:
            with pytest.raises(core.ArchiveError):
                operation(archive)
        destination = tmp_path / f"absent-{cut}"
        with pytest.raises(core.ArchiveError):
            native_engine.extract(archive, destination)
        assert not destination.exists(), (kind, suffix, cut)


@pytest.mark.parametrize("suffix,codec", CODECS[1:])
@pytest.mark.parametrize("command", ["test", "list", "extract"])
def test_cli_rejects_original_pax_header_in_complete_compressed_stream(tmp_path, suffix, codec, command):
    archive = tmp_path / f"writer-cut-next-metadata-header-partial.{suffix}"
    write_variant(archive, pax_repro(), codec)
    destination = tmp_path / "new-destination"
    arguments = [command, str(archive)]
    if command == "extract":
        arguments += ["-C", str(destination)]
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments], cwd=tmp_path,
        env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1", PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    assert str(archive) in result.stderr
    assert "truncated" in result.stderr.lower()
    assert "Traceback" not in result.stderr
    assert not destination.exists()


@pytest.mark.parametrize("suffix,codec", CODECS[1:])
def test_truncated_compression_remains_an_error(tmp_path, native_engine, suffix, codec):
    data, _, _ = writer_fixture("pax")
    compressed = codec.compress(data)
    archive = tmp_path / f"compression-cut.{suffix}"
    for cut in sorted({1, len(compressed) // 2, len(compressed) - 16, len(compressed) - 1}):
        archive.write_bytes(compressed[:cut])
        with pytest.raises(core.ArchiveError):
            native_engine.test(archive)
        destination = tmp_path / f"absent-{cut}"
        with pytest.raises(core.ArchiveError):
            native_engine.extract(archive, destination)
        # Missing compressor trailers may be detected only by the existing
        # extraction pass; header/metadata failures have separate ordering tests.


@pytest.mark.parametrize("suffix", ["tar", "tar.gz", "tar.bz2", "tar.xz"])
def test_native_inspection_opens_tar_once(tmp_path, native_engine, monkeypatch, suffix):
    data, members, _ = writer_fixture("pax")
    codec = dict(CODECS)[suffix]
    archive = tmp_path / f"one-pass.{suffix}"
    write_variant(archive, data, codec)
    original = core.tarfile.open
    calls = []

    def counted(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(core.tarfile, "open", counted)
    verify_inspection(native_engine.inspect(archive), members)
    assert len(calls) == 1


@pytest.mark.parametrize("suffix", ["tar", "tar.gz", "tar.bz2", "tar.xz"])
def test_snug_created_tar_remains_compatible(tmp_path, native_engine, suffix):
    source = tmp_path / "source"
    source.mkdir()
    (source / "short.txt").write_bytes(b"short")
    (source / ("\u540d" * 40 + ".txt")).write_bytes(b"unicode")
    archive = tmp_path / f"snug-created.{suffix}"
    native_engine.create(archive, [source], root=source)
    report = native_engine.test(archive)
    assert report.files == 2
    assert report.bytes_read == len(b"shortunicode")
    destination = tmp_path / "output"
    native_engine.extract(archive, destination)
    for original in source.iterdir():
        assert (destination / original.name).read_bytes() == original.read_bytes()


def test_available_system_tar_writers_remain_compatible(tmp_path, native_engine):
    source = tmp_path / "system-source"
    source.mkdir()
    (source / "short.txt").write_bytes(b"short")
    (source / ("\u540d" * 40 + ".txt")).write_bytes(b"unicode")
    tools = sorted({path for name in ["tar", "bsdtar"] if (path := shutil.which(name))})
    for index, tool in enumerate(tools):
        archive = tmp_path / f"system-{index}.tar"
        result = subprocess.run([tool, "-cf", str(archive), "-C", str(source), "."],
                                env=dict(os.environ, COPYFILE_DISABLE="1"),
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        report = native_engine.test(archive)
        assert report.files == 2
        assert report.bytes_read == len(b"shortunicode")
        destination = tmp_path / f"system-output-{index}"
        native_engine.extract(archive, destination)
        for original in source.iterdir():
            assert (destination / original.name).read_bytes() == original.read_bytes()
