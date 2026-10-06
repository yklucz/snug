import hashlib
import io
import os
from pathlib import Path
import sys
import subprocess
import tarfile

import pytest

import snug


@pytest.mark.parametrize("encrypted", [False, True])
def test_sevenzip_round_trip(engine, source_tree, tmp_path, py7zr_backend, encrypted):
    archive = tmp_path / "sample.7z"
    password = "test-password" if encrypted else None
    engine.create(archive, [source_tree], root=source_tree, password=password)
    # Also inspect with the independent dependency API, so a renamed ZIP does
    # not accidentally count as working 7z creation.
    assert archive.read_bytes()[:6] == b"7z\xbc\xaf'\x1c"
    with py7zr_backend.SevenZipFile(archive, password=password) as handle:
        assert "nested/binary.bin" in handle.getnames()
    entries = engine.list_entries(archive, password=password)
    assert {"hello.txt", "nested/binary.bin"} <= {e.name for e in entries}
    info = engine.info(archive, password=password)
    assert info["format"] == "7z"
    assert info["backend"] == "py7zr"
    assert info["can_create"] is True
    dest = tmp_path / "output"
    report = engine.extract(archive, dest, password=password)
    assert report.files == 3
    assert (dest / "hello.txt").read_bytes() == (source_tree / "hello.txt").read_bytes()
    assert (dest / "nested/binary.bin").read_bytes() == (source_tree / "nested/binary.bin").read_bytes()
    assert (dest / "empty_dir").is_dir()


def test_sevenzip_independent_extraction(engine, tmp_path, py7zr_backend):
    archive = tmp_path / "sample.7z"
    with py7zr_backend.SevenZipFile(archive, "w") as handle:
        handle.writestr(b"independent fixture\n", "nested/hello.txt")
    renamed = archive.with_name("wrong.zip")
    archive.rename(renamed)
    assert snug.detect_format(renamed).value == "7z"
    engine.extract(renamed, tmp_path / "output")
    assert (tmp_path / "output/nested/hello.txt").read_bytes() == b"independent fixture\n"


def test_sevenzip_wrong_password_is_useful_error(engine, tmp_path, py7zr_backend):
    archive = tmp_path / "protected.7z"
    with py7zr_backend.SevenZipFile(archive, "w", password="correct", header_encryption=True) as handle:
        handle.writestr(b"secret", "secret.txt")
    for password in [None, "wrong"]:
        with pytest.raises(snug.ArchiveError, match=r"(?i)(password|decrypt|encrypt)"):
            engine.extract(archive, tmp_path / "output", password=password)
    assert not (tmp_path / "output/secret.txt").exists()


def test_sevenzip_no_overwrite(engine, tmp_path, py7zr_backend):
    archive = tmp_path / "sample.7z"
    with py7zr_backend.SevenZipFile(archive, "w") as handle:
        handle.writestr(b"new", "hello.txt")
    dest = tmp_path / "output"
    dest.mkdir()
    (dest / "hello.txt").write_bytes(b"keep")
    report = engine.extract(archive, dest, overwrite=False)
    assert (dest / "hello.txt").read_bytes() == b"keep"
    assert report.skipped


@pytest.mark.parametrize("name", ["../evil.txt", "/evil.txt", "C:/evil.txt"])
def test_sevenzip_rejects_unsafe_member(engine, tmp_path, py7zr_backend, name):
    archive = tmp_path / "bad.7z"
    with py7zr_backend.SevenZipFile(archive, "w") as handle:
        # py7zr itself rejects bad names in public writestr(); using its
        # low-level writer is solely for creating adversarial test input.
        handle._writestr(b"evil", name)
    with pytest.raises(snug.UnsafeArchiveError):
        engine.extract(archive, tmp_path / "output")
    assert not (tmp_path / "evil.txt").exists()


def test_missing_optional_dependencies_do_not_break_native(engine, tmp_path, monkeypatch):
    # import modules in sys.modules as None reliably simulates absence even
    # when optional dependencies happen to be installed in the test runner.
    monkeypatch.setitem(sys.modules, "py7zr", None)
    monkeypatch.setitem(sys.modules, "libarchive", None)
    source = tmp_path / "file.txt"
    source.write_bytes(b"native still works")
    native = tmp_path / "native.zip"
    engine.create(native, [source])
    engine.extract(native, tmp_path / "native-output")
    assert (tmp_path / "native-output/file.txt").read_bytes() == source.read_bytes()
    with pytest.raises(snug.ArchiveError, match=r"(?i)(py7zr|7z).*(require|install)|requires.*(py7zr|7z)"):
        engine.create(tmp_path / "optional.7z", [source])
    fake_rar = tmp_path / "sample.rar"
    fake_rar.write_bytes(b"Rar!\x1a\x07\x00")
    with pytest.raises(snug.ArchiveError, match=r"(?i)libarchive"):
        engine.list_entries(fake_rar)


READ_FIXTURES = [
    ("test_read_format_rar.rar", "rar", {"test.txt": b"test text document\r\n", "testdir/test.txt": b"test text document\r\n"}),
    ("test_read_format_rar5_compressed.rar", "rar5", {}),
    ("test_read_format_cab_1.cab", "cab", {"empty": b"", "dir1/file1": b"                          file 1 contents\nhello\nhello\nhello\n", "dir2/file2": b"                          file 2 contents\nhello\nhello\nhello\nhello\nhello\nhello\n"}),
    ("test_read_format_iso_2.iso", "iso", {"A/B": b"hello\n", "C/D": b"hello\n"}),
    ("test_read_format_zip_ppmd8.zipx", "zipx", {}),
    ("test_read_format_lha_header0.lzh", "lha", {"file1": b"                          file 1 contents\nhello\nhello\nhello\n", "file2": b"                          file 2 contents\nhello\nhello\nhello\nhello\nhello\nhello\n"}),
    ("test_read_format_warc.warc", "warc", {"sometest.txt": b"This is a sample text file for libarchive's WARC reader/writer.\n\n", "moretest.txt": b"The beauty is that WARC remains ASCII only iff all contents are ASCII only.\n"}),
    ("test_read_format_cpio_svr4_gzip_rpm.rpm", "rpm", {"etc/file1": b"hello\n", "etc/file2": b"hello\n", "etc/file3": b"hello\n"}),
    ("sample.xar", "xar", {"nested/hello.txt": b"XAR round trip\n"}),
]


@pytest.mark.parametrize("fixture_name,expected_format,expected_files", READ_FIXTURES)
def test_real_extended_fixtures(engine, fixture_dir, tmp_path, libarchive_backend, fixture_name, expected_format, expected_files):
    archive = fixture_dir / fixture_name
    entries = engine.list_entries(archive)
    assert entries
    info = engine.info(archive)
    assert info["format"] == expected_format
    assert info["backend"] == "libarchive"
    assert info["can_extract"] is True
    if expected_format in {"rar", "rar5", "cab", "iso", "lha", "lzh", "rpm"}:
        assert info["can_create"] is False
    dest = tmp_path / "output"
    engine.extract(archive, dest, preserve_metadata=False, symlinks="skip" if os.name == "nt" else "store")
    for name, content in expected_files.items():
        assert (dest / name).read_bytes() == content
    if expected_format == "rar5":
        content = (dest / "test.bin").read_bytes()
        assert len(content) == 1200
        # Independently regenerate the payload as documented upstream.
        assert content == b"".join(max(0, k * k - 3 * k + 1).to_bytes(4, "little") for k in range(1, 301))
    if expected_format == "zipx":
        assert (dest / "vimrc").stat().st_size == 912
        assert (dest / "vimrc").read_bytes().startswith(b'" All system-wide defaults')


@pytest.mark.parametrize("fixture_name,expected_format,_", READ_FIXTURES[:5])
def test_extended_detection_with_wrong_suffix(engine, fixture_dir, tmp_path, libarchive_backend, fixture_name, expected_format, _):
    renamed = tmp_path / "incorrect.tar"
    renamed.write_bytes((fixture_dir / fixture_name).read_bytes())
    assert snug.detect_format(renamed).value == expected_format
    assert engine.list_entries(renamed)


@pytest.mark.parametrize("suffix", ["cpio", "ar"])
def test_libarchive_create_round_trip(engine, tmp_path, libarchive_backend, suffix):
    source = tmp_path / "hello.txt"
    source.write_bytes(b"created by Snug\n")
    archive = tmp_path / f"sample.{suffix}"
    engine.create(archive, [source])
    assert engine.info(archive)["can_create"] is True
    engine.extract(archive, tmp_path / "output")
    assert (tmp_path / "output/hello.txt").read_bytes() == source.read_bytes()


def test_ar_deb_container_extraction(engine, tmp_path, libarchive_backend):
    archive = tmp_path / "sample.deb"
    # DEB is an AR container. This fixture makes no claim to package install or
    # automatic extraction of its nested payload/control TAR archives.
    control_data = io.BytesIO()
    with tarfile.open(fileobj=control_data, mode="w:gz") as handle:
        payload = b"Package: snug-fixture\nVersion: 1.0\nArchitecture: all\nMaintainer: Snug test suite\nDescription: local regression data\n"
        item = tarfile.TarInfo("./control")
        item.size = len(payload)
        handle.addfile(item, io.BytesIO(payload))
    payload_data = io.BytesIO()
    with tarfile.open(fileobj=payload_data, mode="w:gz") as handle:
        item = tarfile.TarInfo("./usr/share/snug/hello.txt")
        item.size = 5
        handle.addfile(item, io.BytesIO(b"hello"))
    members = [("debian-binary", b"2.0\n"), ("control.tar.gz", control_data.getvalue()),
               ("data.tar.gz", payload_data.getvalue())]
    data = bytearray(b"!<arch>\n")
    for name, content in members:
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}`\n".encode("ascii")
        data.extend(header + content)
        if len(content) % 2:
            data.extend(b"\n")
    archive.write_bytes(data)
    assert engine.info(archive)["format"] == "deb"
    engine.extract(archive, tmp_path / "output")
    assert (tmp_path / "output/debian-binary").read_bytes() == b"2.0\n"
    assert (tmp_path / "output/control.tar.gz").read_bytes() == control_data.getvalue()
    assert (tmp_path / "output/data.tar.gz").read_bytes() == payload_data.getvalue()


@pytest.mark.parametrize("suffix", ["rar", "rar5", "cab", "iso", "lha", "lzh", "rpm", "deb", "xar", "warc", "zipx"])
def test_read_only_creation_rejected_without_partial_output(engine, tmp_path, suffix):
    source = tmp_path / "file.txt"
    source.write_text("hello")
    archive = tmp_path / f"new.{suffix}"
    with pytest.raises(snug.ArchiveError, match=r"(?i)(creat|writ|read.only)"):
        engine.create(archive, [source])
    assert not archive.exists()


@pytest.mark.parametrize("suffix", ["lha", "lzh"])
def test_lha_lzh_aliases(engine, fixture_dir, tmp_path, libarchive_backend, suffix):
    archive = tmp_path / f"sample.{suffix}"
    archive.write_bytes((fixture_dir / "test_read_format_lha_header0.lzh").read_bytes())
    assert engine.list_entries(archive)
    dest = tmp_path / "output"
    engine.extract(archive, dest, preserve_metadata=False, symlinks="skip" if os.name == "nt" else "store")
    assert (dest / "file1").read_bytes().startswith(b"                          file 1 contents\n")


def test_fixture_checksums(fixture_dir):
    import json

    for row in json.loads((fixture_dir / "manifest.json").read_text()):
        data = (fixture_dir / row["name"]).read_bytes()
        assert len(data) == row["size"]
        assert hashlib.sha256(data).hexdigest() == row["sha256"]


def test_sevenzip_existing_symlink_parent(engine, tmp_path, py7zr_backend):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "output"
    dest.mkdir()
    try:
        (dest / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    archive = tmp_path / "sample.7z"
    with py7zr_backend.SevenZipFile(archive, "w") as handle:
        handle.writestr(b"evil", "link/new/evil.txt")
    with pytest.raises(snug.UnsafeArchiveError):
        engine.extract(archive, dest)
    assert list(outside.iterdir()) == []


def test_sevenzip_safe_symlink_round_trip(engine, tmp_path, py7zr_backend):
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_bytes(b"safe link")
    try:
        (source / "link").symlink_to("hello.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    archive = tmp_path / "sample.7z"
    engine.create(archive, [source], root=source)
    dest = tmp_path / "output"
    engine.extract(archive, dest)
    assert (dest / "link").is_symlink()
    assert (dest / "link").read_bytes() == b"safe link"


def test_cpio_safe_symlink_round_trip(engine, tmp_path, libarchive_backend):
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_bytes(b"safe cpio link")
    try:
        (source / "link").symlink_to("hello.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    archive = tmp_path / "sample.cpio"
    engine.create(archive, [source], root=source)
    entries = {entry.name: entry for entry in engine.list_entries(archive)}
    assert entries["link"].is_symlink
    assert entries["link"].link_target == "hello.txt"
    dest = tmp_path / "output"
    engine.extract(archive, dest)
    assert (dest / "link").is_symlink()
    assert (dest / "link").read_bytes() == b"safe cpio link"


def test_sevenzip_escaping_symlink(engine, tmp_path, py7zr_backend):
    outside = tmp_path / "outside"
    outside.mkdir()
    source = tmp_path / "source/escape"
    source.parent.mkdir()
    try:
        source.symlink_to("../outside")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    archive = tmp_path / "bad.7z"
    with py7zr_backend.SevenZipFile(archive, "w") as handle:
        handle.write(source, "escape")
    assert engine.list_entries(archive)[0].is_symlink
    with pytest.raises(snug.UnsafeArchiveError):
        engine.extract(archive, tmp_path / "output")
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_NOFILE is a Unix-only diagnostic")
def test_sevenzip_many_files_does_not_leak_descriptors(tmp_path, py7zr_backend):
    archive = tmp_path / "many.7z"
    with py7zr_backend.SevenZipFile(archive, "w") as handle:
        for index in range(300):
            handle.writestr(b"data", f"files/{index}.txt")
    script = """
import resource, sys
from pathlib import Path
import snug
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(128, soft), hard))
dest = Path(sys.argv[2])
engine = snug.ArchiveEngine()
report = engine.extract(sys.argv[1], dest)
assert report.files == 300
assert all((dest / f'files/{i}.txt').read_bytes() == b'data' for i in range(300))
again = engine.extract(sys.argv[1], dest, overwrite=False)
assert again.files == 0 and len(again.skipped) == 300
"""
    result = subprocess.run([sys.executable, "-c", script, str(archive), str(tmp_path / "output")],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


def test_thin_ar_external_references_are_rejected(engine, tmp_path, libarchive_backend):
    archive = tmp_path / "thin.ar"
    archive.write_bytes(b"!<thin>\n")
    with pytest.raises(snug.ArchiveError, match=r"(?i)thin"):
        engine.list_entries(archive)
    with pytest.raises(snug.ArchiveError, match=r"(?i)thin"):
        engine.extract(archive, tmp_path / "output")


def test_libarchive_sevenzip_fallback_without_py7zr(engine, fixture_dir, tmp_path, libarchive_backend, monkeypatch):
    monkeypatch.setitem(sys.modules, "py7zr", None)
    archive = fixture_dir / "test_read_format_7zip_lzma1_2.7z"
    entries = engine.list_entries(archive)
    assert len(entries) == 5
    assert engine.info(archive)["backend"] == "libarchive"
    dest = tmp_path / "output"
    engine.extract(archive, dest)
    assert (dest / "dir1/file1").read_bytes() == b"aaaaaaaaaaaa\n"
    assert (dest / "file4").read_bytes() == b"aaaaaaaaaaaa\nbbbbbbbbbbbb\ncccccccccccc\ndddddddddddd\n"


def test_truncated_rar5_header_is_rejected(engine, tmp_path, libarchive_backend):
    archive = tmp_path / 'truncated.rar5'
    archive.write_bytes(b'Rar!\x1a\x07\x01\x00broken')
    for operation in (engine.list_entries, engine.info):
        with pytest.raises(snug.ArchiveError, match='truncated RAR5'):
            operation(archive)
    with pytest.raises(snug.ArchiveError, match='truncated RAR5'):
        engine.extract(archive, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()
