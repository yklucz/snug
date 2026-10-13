"""Recovery advice must retain native create policy and unsafe-link outcomes."""

import builtins
import errno
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

import snug
import snug_core as core


SCRIPT = Path(__file__).resolve().parents[1] / "snug.py"


@pytest.mark.parametrize("suffix", ["zip", "tar", "tar.gz"])
@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("case, expected", [("all", 2), ("existing", 2), ("partial", 5)])
def test_native_create_advice_preserves_publication_policy(tmp_path, monkeypatch, capsys,
                                                        suffix, quiet, case, expected):
    readable = tmp_path / "readable.txt"
    unreadable = tmp_path / "unreadable.txt"
    readable.write_bytes(b"readable")
    unreadable.write_bytes(b"unreadable")
    archive = tmp_path / f"output.{suffix}"
    previous = b"existing archive bytes"
    if case == "existing":
        archive.write_bytes(previous)
    original = builtins.open

    def deny_source(file, mode="r", *args, **kwargs):
        if not isinstance(file, int) and Path(file) == unreadable and "r" in mode:
            raise PermissionError(errno.EACCES, "Permission denied", str(file))
        return original(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny_source)
    monkeypatch.setattr(snug, "_start_update_check", lambda: None)
    engine = core.ArchiveEngine([core.NativeBackend()])
    monkeypatch.setattr(snug, "ArchiveEngine", lambda: engine)
    chosen = [unreadable] if case == "all" else [readable, unreadable]
    args = ["create", str(archive), *map(str, chosen)]
    args += ["--force"] if case == "existing" else []
    args += ["-q"] if quiet else []
    assert snug.main(args) == expected
    result = capsys.readouterr()
    assert "check file permissions or exclude unreadable inputs" in result.err
    assert "permission denied" in result.err.lower() and str(unreadable) in result.err
    if case == "existing":
        assert "the existing archive was preserved" in result.err
        assert archive.read_bytes() == previous
    elif case == "all":
        assert "no archive was created" in result.err
        assert not archive.exists()
    else:
        assert "1 included, 1 skipped" in result.err
        assert [entry.name for entry in engine.list_entries(archive)] == ["readable.txt"]
    assert not list(tmp_path.glob(f".{archive.name}.*.part"))


@pytest.mark.parametrize("quiet", [False, True])
def test_directory_conflict_advice_preserves_existing_directory(tmp_path, quiet):
    archive = tmp_path / "conflict.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("first.txt", b"payload")
    destination = tmp_path / "output"
    conflict = destination / "first.txt"
    conflict.mkdir(parents=True)
    sentinel = conflict / "keep.txt"
    sentinel.write_bytes(b"preserved")
    args = [sys.executable, "-B", str(SCRIPT), "extract", str(archive), "-C", str(destination)]
    args += ["-q"] if quiet else []
    result = subprocess.run(args, env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1"),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2 and not result.stdout
    assert "cannot atomically replace directory with file: 'first.txt'" in result.stderr
    assert "extract into an empty directory" in result.stderr
    assert "rename or remove the existing directory" in result.stderr
    assert sentinel.read_bytes() == b"preserved"
    assert not list(destination.glob(".snug-part-*"))


@pytest.mark.parametrize("target", ["../outside.txt", "/absolute/outside.txt", "C:/outside.txt"])
@pytest.mark.parametrize("command", ["test", "extract"])
def test_unsafe_hardlink_error_names_origin_and_target(tmp_path, target, command):
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo("originating-link")
        member.type = tarfile.LNKTYPE
        member.linkname = target
        output.addfile(member)
    destination = tmp_path / "output"
    args = [sys.executable, "-B", str(SCRIPT), command, str(archive)]
    args += ["-C", str(destination)] if command == "extract" else []
    result = subprocess.run(args, env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1"),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 3 and not result.stdout
    assert result.stderr.startswith("error: unsafe archive:")
    assert "originating-link" in result.stderr and target in result.stderr
    assert not destination.exists()
