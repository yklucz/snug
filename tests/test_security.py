import io
import os
from pathlib import Path
import stat
import tarfile
import zipfile

import pytest

import snug


def write_archive(path, entries):
    """Create adversarial members independently of Snug's creation code."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path, "w") as archive:
            for name, kind, value in entries:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                if kind == "symlink":
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                else:
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, value)
    else:
        with tarfile.open(path, "w") as archive:
            for name, kind, value in entries:
                info = tarfile.TarInfo(name)
                info.mode = 0o644
                if kind in {"symlink", "hardlink"}:
                    info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                    info.linkname = value
                    archive.addfile(info)
                else:
                    info.size = len(value)
                    archive.addfile(info, io.BytesIO(value))


@pytest.mark.parametrize("suffix", ["zip", "tar"])
@pytest.mark.parametrize("name", ["../evil.txt", "/absolute/evil.txt", "C:/evil.txt", "C:\\evil.txt", "\\\\server\\share\\evil.txt", "nested/../../evil.txt"])
def test_unsafe_member_paths(engine, tmp_path, suffix, name):
    archive = tmp_path / f"bad.{suffix}"
    write_archive(archive, [(name, "file", b"evil")])
    with pytest.raises(snug.UnsafeArchiveError):
        engine.extract(archive, tmp_path / "output")
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "output/evil.txt").exists()


@pytest.mark.parametrize("suffix", ["zip", "tar"])
def test_absolute_actual_path_never_written(engine, tmp_path, suffix):
    archive = tmp_path / f"bad.{suffix}"
    outside = tmp_path / "outside/created/evil.txt"
    write_archive(archive, [(str(outside), "file", b"evil")])
    with pytest.raises(snug.UnsafeArchiveError):
        engine.extract(archive, tmp_path / "output")
    assert not outside.parent.exists()


@pytest.mark.parametrize("suffix", ["zip", "tar"])
@pytest.mark.parametrize("target", ["../outside", "/tmp/snug-outside", "C:/outside", "C:\\outside"])
def test_escaping_archive_symlink(engine, tmp_path, suffix, target):
    archive = tmp_path / f"bad.{suffix}"
    write_archive(archive, [("escape", "symlink", target), ("escape/sub/evil.txt", "file", b"evil")])
    with pytest.raises(snug.UnsafeArchiveError):
        engine.extract(archive, tmp_path / "output")
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("suffix", ["zip", "tar"])
def test_existing_nested_symlink_parent_does_not_create_outside_dirs(engine, tmp_path, suffix):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "output"
    dest.mkdir()
    try:
        (dest / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    archive = tmp_path / f"bad.{suffix}"
    write_archive(archive, [("link/new/sub/evil.txt", "file", b"evil")])
    with pytest.raises(snug.UnsafeArchiveError):
        engine.extract(archive, dest)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("target", ["../outside.txt", "/absolute/outside.txt", "C:/outside.txt"])
def test_hardlink_target_traversal(engine, tmp_path, target):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"original")
    archive = tmp_path / "bad.tar"
    write_archive(archive, [("link", "hardlink", target)])
    with pytest.raises(snug.UnsafeArchiveError):
        engine.extract(archive, tmp_path / "output")
    assert outside.read_bytes() == b"original"
    assert not (tmp_path / "output/link").exists()


def test_hardlink_source_existing_symlink_rejected(engine, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"original")
    dest = tmp_path / "output"
    dest.mkdir()
    try:
        (dest / "source").symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    archive = tmp_path / "bad.tar"
    write_archive(archive, [("copy", "hardlink", "source")])
    with pytest.raises(snug.UnsafeArchiveError):
        engine.extract(archive, dest)
    assert not (dest / "copy").exists()


def test_safe_hardlinks_and_symlinks(engine, tmp_path):
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to("file")
        probe.unlink()
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    archive = tmp_path / "safe.tar"
    write_archive(archive, [("file", "file", b"safe"), ("hard", "hardlink", "file"), ("soft", "symlink", "file")])
    dest = tmp_path / "output"
    engine.extract(archive, dest)
    assert (dest / "hard").read_bytes() == b"safe"
    assert (dest / "soft").is_symlink()
    assert (dest / "soft").read_bytes() == b"safe"


@pytest.mark.parametrize("suffix", ["zip", "tar", "gz", "bz2", "xz", "lzma"])
def test_no_overwrite_preserves_existing(engine, tmp_path, suffix):
    source = tmp_path / "payload"
    source.write_bytes(b"new contents")
    archive = tmp_path / f"payload.{suffix}"
    engine.create(archive, [source])
    dest = tmp_path / "output"
    dest.mkdir()
    (dest / "payload").write_bytes(b"keep")
    report = engine.extract(archive, dest, overwrite=False)
    assert (dest / "payload").read_bytes() == b"keep"
    assert report.files == 0
    assert report.skipped


@pytest.mark.parametrize("suffix", ["zip", "tar"])
def test_no_overwrite_existing_symlink(engine, tmp_path, suffix):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")
    dest = tmp_path / "output"
    dest.mkdir()
    try:
        (dest / "payload").symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    archive = tmp_path / f"payload.{suffix}"
    write_archive(archive, [("payload", "file", b"evil")])
    try:
        engine.extract(archive, dest, overwrite=False)
    except snug.UnsafeArchiveError:
        pass  # Rejecting the untrusted destination link is also safe.
    assert outside.read_bytes() == b"keep"
    assert (dest / "payload").is_symlink()


def test_libarchive_cpio_rejects_traversal(engine, tmp_path, libarchive_backend):
    archive = tmp_path / "bad.cpio"
    with libarchive_backend.file_writer(str(archive), "cpio_newc") as handle:
        handle.add_file_from_memory("../evil.txt", 4, b"evil")
    with pytest.raises(snug.UnsafeArchiveError):
        engine.extract(archive, tmp_path / "output")
    assert not (tmp_path / "evil.txt").exists()


def test_libarchive_cpio_rejects_symlink_parent(engine, tmp_path, libarchive_backend):
    archive = tmp_path / "bad.cpio"
    # Build newc records directly: symlink destinations are the record payload,
    # and the generic libarchive-c linkpath setter selects a hardlink instead.
    data = bytearray()
    for name, mode, payload in [("escape", stat.S_IFLNK | 0o777, b"../outside"),
                                ("escape/new/evil.txt", stat.S_IFREG | 0o644, b"evil"),
                                ("TRAILER!!!", 0, b"")]:
        encoded_name = name.encode("ascii") + b"\0"
        fields = [1, mode, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(encoded_name), 0]
        record = b"070701" + b"".join(f"{field:08x}".encode("ascii") for field in fields) + encoded_name
        data.extend(record + b"\0" * (-len(record) % 4))
        data.extend(payload + b"\0" * (-len(payload) % 4))
    archive.write_bytes(data)
    entries = engine.list_entries(archive)
    assert entries[0].is_symlink and entries[0].link_target == "../outside"
    with pytest.raises(snug.UnsafeArchiveError):
        engine.extract(archive, tmp_path / "output")
    assert not (tmp_path / "outside").exists()


def test_self_hardlink_preserves_existing_source(engine, tmp_path):
    archive = tmp_path / "self.tar"
    write_archive(archive, [("file", "hardlink", "file")])
    dest = tmp_path / "output"
    dest.mkdir()
    source = dest / "file"
    source.write_bytes(b"keep this file")
    try:
        engine.extract(archive, dest)
    except snug.UnsafeArchiveError:
        pass
    assert source.read_bytes() == b"keep this file"
