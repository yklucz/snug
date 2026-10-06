import bz2
import gzip
import io
import lzma
from pathlib import Path
import tarfile
import zipfile

import pytest

import snug


NATIVE_FORMATS = [
    ("zip", "zip"), ("tar", "tar"), ("tar.gz", "tar.gz"),
    ("tgz", "tar.gz"), ("tar.bz2", "tar.bz2"), ("tbz2", "tar.bz2"),
    ("tbz", "tar.bz2"), ("tar.xz", "tar.xz"), ("txz", "tar.xz"),
]


@pytest.mark.parametrize("suffix,expected_format", NATIVE_FORMATS)
def test_native_round_trip(engine, source_tree, tmp_path, suffix, expected_format):
    archive = tmp_path / f"archive.{suffix}"
    report = engine.create(archive, [source_tree], root=source_tree)
    assert report.bytes_in >= 1024 * 1024
    assert report.archive == archive
    assert report.format.value == expected_format
    entries = {e.name.rstrip("/"): e for e in engine.list_entries(archive)}
    assert {"hello.txt", "nested/binary.bin", "empty.txt", "empty_dir"} <= entries.keys()
    assert entries["empty_dir"].is_dir
    info = engine.info(archive)
    assert info["format"] == expected_format
    assert info["backend"] == "native"
    assert info["can_extract"] is True
    assert info["can_create"] is True
    assert info["files"] == 3
    assert info["uncompressed_size"] == 1024 * 1024 + len(b"hello Snug\n")
    dest = tmp_path / "output"
    extracted = engine.extract(archive, dest)
    assert extracted.files == 3
    assert (dest / "empty_dir").is_dir()
    for relative in ["hello.txt", "nested/binary.bin", "empty.txt"]:
        assert (dest / relative).read_bytes() == (source_tree / relative).read_bytes()


STREAMS = [("gz", "gz", gzip), ("bz2", "bz2", bz2),
           ("xz", "xz", lzma), ("lzma", "lzma", lzma)]


@pytest.mark.parametrize("suffix,expected_format,codec", STREAMS)
def test_stream_round_trip(engine, tmp_path, suffix, expected_format, codec):
    source = tmp_path / "payload.txt"
    content = b"single stream\n" * 16384
    source.write_bytes(content)
    archive = tmp_path / f"payload.txt.{suffix}"
    report = engine.create(archive, [source])
    assert report.files == 1
    assert snug.detect_format(archive).value == expected_format
    assert codec.decompress(archive.read_bytes()) == content
    entries = engine.list_entries(archive)
    assert len(entries) == 1
    assert entries[0].name == "payload.txt"
    assert entries[0].size == len(content)
    info = engine.info(archive)
    assert info["format"] == expected_format
    assert info["can_create"] is True
    dest = tmp_path / "output"
    engine.extract(archive, dest)
    assert (dest / "payload.txt").read_bytes() == content


@pytest.mark.parametrize("suffix", ["gz", "bz2", "xz", "lzma"])
@pytest.mark.parametrize("invalid", ["directory", "multiple"])
def test_stream_rejects_non_single_file(engine, source_tree, tmp_path, suffix, invalid):
    archive = tmp_path / f"output.{suffix}"
    sources = [source_tree] if invalid == "directory" else [source_tree / "hello.txt", source_tree / "empty.txt"]
    with pytest.raises(snug.ArchiveError, match=r"(?i)(single|one|directory|file)"):
        engine.create(archive, sources)
    assert not archive.exists()


@pytest.mark.parametrize("suffix,expected_format", NATIVE_FORMATS + [("gz", "gz"), ("bz2", "bz2"), ("xz", "xz"), ("lzma", "lzma")])
@pytest.mark.parametrize("renamed", ["archive", "archive.wrong", "archive.rar"])
def test_detection_prefers_contents(engine, source_tree, tmp_path, suffix, expected_format, renamed):
    original = tmp_path / f"original.{suffix}"
    sources = [source_tree / "hello.txt"] if suffix in {"gz", "bz2", "xz", "lzma"} else [source_tree]
    engine.create(original, sources, root=source_tree)
    target = tmp_path / renamed
    original.rename(target)
    assert snug.detect_format(target).value == expected_format
    assert engine.list_entries(target)
    dest = tmp_path / "output"
    engine.extract(target, dest)
    assert any(p.is_file() for p in dest.rglob("*"))


@pytest.mark.parametrize("suffix", ["zip", "tar", "tar.gz", "tar.bz2", "tar.xz"])
def test_member_selection_and_strip(engine, source_tree, tmp_path, suffix):
    archive = tmp_path / f"archive.{suffix}"
    engine.create(archive, [source_tree])
    dest = tmp_path / "output"
    engine.extract(archive, dest, members=["source/nested/binary.bin"], strip_components=1)
    assert (dest / "nested/binary.bin").read_bytes() == (source_tree / "nested/binary.bin").read_bytes()
    assert not (dest / "hello.txt").exists()


class RecordingProgress:
    def __init__(self):
        self.started = []
        self.names = []
        self.bytes = 0
        self.finished = False

    def start(self, total_bytes, total_items):
        self.started.append((total_bytes, total_items))

    def item(self, name):
        self.names.append(name)

    def chunk(self, size):
        self.bytes += size

    def done(self):
        self.finished = True


@pytest.mark.parametrize("suffix", ["zip", "tar.gz", "gz"])
def test_byte_progress(engine, source_tree, tmp_path, suffix):
    source = source_tree / "nested" / "binary.bin"
    archive = tmp_path / f"payload.{suffix}"
    create_progress = RecordingProgress()
    engine.create(archive, [source], progress=create_progress)
    assert create_progress.started == [(source.stat().st_size, 1)]
    assert create_progress.bytes == source.stat().st_size
    assert create_progress.finished
    extract_progress = RecordingProgress()
    report = engine.extract(archive, tmp_path / "output", progress=extract_progress)
    assert extract_progress.bytes == report.bytes_written == source.stat().st_size
    assert extract_progress.names
    assert extract_progress.finished


def test_empty_zip_and_bad_input(engine, tmp_path):
    archive = tmp_path / "empty.bin"
    with zipfile.ZipFile(archive, "w"):
        pass
    assert snug.detect_format(archive).value == "zip"
    assert engine.list_entries(archive) == []
    with pytest.raises(snug.ArchiveError):
        engine.list_entries(tmp_path / "missing.zip")
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"PK\x03\x04not a zip")
    with pytest.raises(snug.ArchiveError):
        engine.list_entries(bad)


def test_compressed_tar_detection_does_not_read_entire_member(engine, tmp_path):
    # An independent stdlib-created TAR confirms Snug does not classify every
    # gzip magic header as a standalone stream, including a suffixless input.
    archive = tmp_path / "suffixless"
    with tarfile.open(archive, "w:gz") as handle:
        item = tarfile.TarInfo("tiny.txt")
        item.size = 5
        handle.addfile(item, io.BytesIO(b"tiny\n"))
    assert snug.detect_format(archive).value == "tar.gz"
    engine.extract(archive, tmp_path / "output")
    assert (tmp_path / "output/tiny.txt").read_bytes() == b"tiny\n"


@pytest.mark.parametrize("suffix,codec", [("gz", gzip), ("bz2", bz2), ("xz", lzma)])
def test_zero_filled_stream_is_not_an_empty_tar(engine, tmp_path, suffix, codec):
    content = b"\0" * 4096 + b"not an empty tar"
    archive = tmp_path / f"zero.bin.{suffix}"
    archive.write_bytes(codec.compress(content))
    assert snug.detect_format(archive).value == suffix
    engine.extract(archive, tmp_path / "output")
    assert (tmp_path / "output/zero.bin").read_bytes() == content


def test_unreadable_source_directory_preserves_existing_archive(engine, tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    archive = tmp_path / 'existing.zip'
    archive.write_bytes(b'existing archive stays intact')
    original = Path.iterdir

    def denied(path):
        if path == source:
            raise PermissionError('directory is not readable')
        return original(path)

    monkeypatch.setattr(Path, 'iterdir', denied)
    with pytest.raises(snug.ArchiveError, match='cannot enumerate directory'):
        engine.create(archive, [source])
    assert archive.read_bytes() == b'existing archive stays intact'
    assert not list(tmp_path.glob('.*.part'))


def test_native_zip_password_decryption(engine, fixture_dir, tmp_path):
    archive = fixture_dir / 'sample-traditional.zip'
    info = engine.info(archive)
    assert info['backend'] == 'native'
    assert info['encrypted'] is True
    for password in (None, 'incorrect'):
        with pytest.raises(snug.ArchiveError, match=r'(?i)password'):
            engine.extract(archive, tmp_path / 'wrong', password=password)
    engine.extract(archive, tmp_path / 'output', password='fixture-password')
    assert (tmp_path / 'output/hello.txt').read_bytes() == b'ZIP password fixture\n'


def test_streamed_zip_honors_compression_level(engine, tmp_path):
    source = tmp_path / 'payload.txt'
    payload = b'compressible archive contents\n' * 8192
    source.write_bytes(payload)
    sizes = {}
    for level in (0, 9):
        archive = tmp_path / f'level-{level}.zip'
        engine.create(archive, [source], compresslevel=level)
        with zipfile.ZipFile(archive) as reader:
            assert reader.read('payload.txt') == payload
            sizes[level] = reader.getinfo('payload.txt').compress_size
    assert sizes[9] < sizes[0] // 10
