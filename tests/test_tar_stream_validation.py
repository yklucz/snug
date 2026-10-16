"""Physical TAR validation preserves extended-size and sparse framing."""

import gzip
import io
import lzma
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

import snug_core as core


def _pad(payload):
    return payload + b"\0" * (-len(payload) % 512)


def _header(name, size, kind=tarfile.REGTYPE):
    member = tarfile.TarInfo(name)
    member.type = kind
    member.size = size
    return member.tobuf(format=tarfile.GNU_FORMAT)


def _pax_record(key, value):
    suffix = f" {key}={value}\n".encode()
    size = len(suffix) + 1
    while len(str(size)) + len(suffix) != size:
        size = len(str(size)) + len(suffix)
    return str(size).encode() + suffix


def _pax(fields, kind=tarfile.XHDTYPE):
    payload = b"".join(_pax_record(key, value) for key, value in fields.items())
    return _header("PaxHeaders/entry", len(payload), kind) + _pad(payload)


def _pax_sizes():
    first = _header("first", 3) + _pad(b"one")
    global_record = _pax({"size": "3"}, tarfile.XGLTYPE)
    local_record = _pax({"size": "1025", "comment": "x" * 1300})
    second = _header("second", 0) + _pad(b"x" * 1025)
    third = _header("third", 3) + _pad(b"end")
    return first + global_record + local_record + second + third


def _pax_sparse(version):
    fields = {"GNU.sparse.realsize": "9999"}
    payload = b"abcxyz"
    if version == "0.1":
        fields["GNU.sparse.map"] = "0,3,9996,3"
    else:
        fields.update({"GNU.sparse.major": "1", "GNU.sparse.minor": "0"})
        payload = _pad(b"2\n0\n3\n9996\n3\n") + payload
    return _pax(fields) + _header("sparse", len(payload)) + _pad(payload)


def _old_gnu_sparse():
    header = bytearray(_header("sparse", 6, tarfile.GNUTYPE_SPARSE))
    header[386:410] = b"00000000000\0" + b"00000000003\0"
    header[482] = 1
    header[483:495] = b"00000023417\0"  # 9999 logical bytes.
    header[148:156] = b"        "
    header[148:156] = f"{sum(header):06o}\0 ".encode()
    extra = bytearray(512)
    extra[:24] = b"00000023414\0" + b"00000000003\0"
    return bytes(header) + bytes(extra) + _pad(b"abcxyz")


@pytest.mark.parametrize("compressed", [False, True])
def test_pax_local_size_override_and_global_size_have_physical_boundaries(tmp_path, compressed):
    data = _pax_sizes()
    archive = tmp_path / ("sizes.tar.gz" if compressed else "sizes.tar")
    archive.write_bytes(gzip.compress(data) if compressed else data)
    engine = core.ArchiveEngine([core.NativeBackend()])
    assert [(entry.name, entry.size) for entry in engine.inspect(archive).entries] == [
        ("first", 3), ("second", 1025), ("third", 3),
    ]
    destination = tmp_path / "output"
    engine.extract(archive, destination)
    assert (destination / "second").read_bytes() == b"x" * 1025
    assert (destination / "third").read_bytes() == b"end"
    for cut in (1025, 1535, 1536, 1799, 2048, 2559, 3584, 4096, 4599):
        archive.write_bytes(gzip.compress(data[:cut]) if compressed else data[:cut])
        with pytest.raises(core.ArchiveError):
            engine.inspect(archive)


@pytest.mark.parametrize("kind", ["pax-0.1", "pax-1.0", "gnu-old"])
@pytest.mark.parametrize("compressed", [False, True])
def test_sparse_records_use_physical_stored_size(tmp_path, kind, compressed):
    record = (_old_gnu_sparse() if kind == "gnu-old"
              else _pax_sparse(kind.removeprefix("pax-")))
    first = _header("first", 3) + _pad(b"one")
    final = _header("final", 3) + _pad(b"end")
    data = first + record + final
    archive = tmp_path / ("sparse.tar.gz" if compressed else "sparse.tar")
    archive.write_bytes(gzip.compress(data) if compressed else data)
    engine = core.ArchiveEngine([core.NativeBackend()])
    assert [(entry.name, entry.size) for entry in engine.inspect(archive).entries] == [
        ("first", 3), ("sparse", 9999), ("final", 3),
    ]
    assert engine.test(archive).bytes_read == 10005
    for cut in (1025, 1300, 1535, len(first + record) - 1):
        archive.write_bytes(gzip.compress(data[:cut]) if compressed else data[:cut])
        with pytest.raises(core.ArchiveError):
            engine.inspect(archive)


def test_compressed_inspection_discards_forward_data_in_bounded_reads(tmp_path, monkeypatch):
    payload = b"x" * (core.CHUNK_SIZE * 3 + 7)
    data = (_pax({"comment": "x" * (core.CHUNK_SIZE * 2 + 7)})
            + _header("payload", len(payload)) + _pad(payload) + b"\0" * 1024)
    archive = tmp_path / "bounded.tar.gz"
    archive.write_bytes(gzip.compress(data))
    reads = []
    decoded_bytes = []
    original = gzip.GzipFile.read

    def recorded(source, size=-1):
        reads.append(size)
        data = original(source, size)
        decoded_bytes.append(len(data))
        return data

    monkeypatch.setattr(gzip.GzipFile, "read", recorded)
    entries = core.NativeBackend().inspect(archive).entries
    assert [(entry.name, entry.size) for entry in entries] == [("payload", len(payload))]
    assert all(0 <= size <= core.CHUNK_SIZE for size in reads), reads
    assert sum(decoded_bytes) == len(data)


def test_lzma_alone_native_tar_inspection_preserves_stdlib_decoding(tmp_path):
    data = _header("payload", 3) + _pad(b"one") + b"\0" * 1024
    archive = tmp_path / "renamed.tar"
    archive.write_bytes(lzma.compress(data, format=lzma.FORMAT_ALONE))
    assert [(entry.name, entry.size) for entry in core.NativeBackend().inspect(archive).entries] == [
        ("payload", 3),
    ]


def test_gnu_longname_v7_followup_keeps_stdlib_member_enumeration(tmp_path):
    longname = b"actual-file-name\0"
    data = (_header("././@LongLink", len(longname), tarfile.GNUTYPE_LONGNAME)
            + _pad(longname) + _header("placeholder/", 3, tarfile.AREGTYPE)
            + _pad(b"one") + _header("final", 3) + _pad(b"end")
            + b"\0" * 1024)
    archive = tmp_path / "v7-longname.tar"
    archive.write_bytes(data)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as source:
        expected = [(member.name, member.size, member.isdir()) for member in source.getmembers()]
    entries = core.NativeBackend().inspect(archive).entries
    assert [(entry.name, entry.size, entry.is_dir) for entry in entries] == expected


def test_global_only_pax_size_keeps_stdlib_raw_header_offsets(tmp_path):
    data = (_pax({"size": "1024"}, tarfile.XGLTYPE)
            + _header("first", 1) + _pad(b"a")
            + _header("second", 1) + _pad(b"b") + b"\0" * 1024)
    archive = tmp_path / "global-size.tar"
    archive.write_bytes(data)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as source:
        expected = [(member.name, member.size) for member in source.getmembers()]
    assert expected == [("first", 1024), ("second", 1024)]
    entries = core.NativeBackend().inspect(archive).entries
    assert [(entry.name, entry.size) for entry in entries] == expected


@pytest.mark.parametrize("command", ["test", "extract"])
def test_invalid_complete_pax_without_following_member_is_rejected(tmp_path, command):
    data = (_header("first", 1) + _pad(b"a")
            + _header("PaxHeaders/missing-member", 1, tarfile.XHDTYPE) + _pad(b"x"))
    archive = tmp_path / "invalid-pax-without-member.tar"
    archive.write_bytes(data)
    destination = tmp_path / "absent"
    arguments = [command, str(archive)]
    if command == "extract":
        arguments += ["-C", str(destination)]
    script = Path(__file__).resolve().parents[1] / "snug.py"
    result = subprocess.run(
        [sys.executable, str(script), *arguments], capture_output=True, text=True,
        env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1", PYTHONDONTWRITEBYTECODE="1"),
        timeout=10,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    assert str(archive) in result.stderr
    assert "truncated" in result.stderr.lower()
    assert "Traceback" not in result.stderr
    assert not destination.exists()
