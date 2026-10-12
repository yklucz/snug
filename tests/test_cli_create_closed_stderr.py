"""Published partial archives retain their status when warnings cannot be read."""

import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

import snug


SCRIPT = Path(snug.__file__)


def run_create_with_closed_stderr(arguments, *, unbuffered):
    command = [sys.executable, "-B"]
    if unbuffered:
        command.append("-u")
    command += [str(SCRIPT), "create", *map(str, arguments)]
    reader, writer = os.pipe()
    os.close(reader)
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=writer,
            env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1", NO_COLOR="1"),
        )
    finally:
        os.close(writer)
    try:
        output, _ = process.communicate(timeout=30)
        return process.returncode, output
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


@pytest.mark.parametrize("suffix", ["zip", "tar"])
@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("unbuffered", [False, True])
def test_published_incomplete_create_keeps_exit_five_with_closed_stderr(
        tmp_path, suffix, quiet, unbuffered):
    readable = tmp_path / "readable.txt"
    unreadable = tmp_path / "unreadable.txt"
    readable.write_bytes(b"included payload")
    unreadable.write_bytes(b"omitted payload")
    archive = tmp_path / f"partial.{suffix}"
    previous_mode = unreadable.stat().st_mode
    try:
        unreadable.chmod(0)
        code, output = run_create_with_closed_stderr(
            [archive, readable, unreadable, *(["-q"] if quiet else [])],
            unbuffered=unbuffered,
        )
    finally:
        unreadable.chmod(previous_mode)
    assert code == 5, output.decode()
    if quiet:
        assert output == b""
    else:
        assert b"Created " in output
    if suffix == "zip":
        with zipfile.ZipFile(archive) as result:
            assert result.namelist() == [readable.name]
            assert result.read(readable.name) == b"included payload"
            assert result.testzip() is None
    else:
        with tarfile.open(archive) as result:
            assert result.getnames() == [readable.name]
            payload = result.extractfile(readable.name)
            assert payload is not None
            assert payload.read() == b"included payload"
    assert not list(tmp_path.glob(f".{archive.name}.*.part"))


@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("unbuffered", [False, True])
def test_failed_create_keeps_exit_one_when_its_error_pipe_is_closed(
        tmp_path, quiet, unbuffered):
    unreadable = tmp_path / "unreadable.txt"
    unreadable.write_bytes(b"omitted payload")
    archive = tmp_path / "failed.zip"
    previous_mode = unreadable.stat().st_mode
    try:
        unreadable.chmod(0)
        code, output = run_create_with_closed_stderr(
            [archive, unreadable, *(["-q"] if quiet else [])],
            unbuffered=unbuffered,
        )
    finally:
        unreadable.chmod(previous_mode)
    assert code == 1
    assert output == b""
    assert not archive.exists()
    assert not list(tmp_path.glob(f".{archive.name}.*.part"))


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_cli_create_warning_does_not_swallow_other_io_errors(monkeypatch, failure):
    class UnwritableStream:
        def write(self, text):
            if failure == "write":
                raise OSError("fixture write failure")
            return len(text)

        def flush(self):
            raise OSError("fixture flush failure")

    monkeypatch.setattr(sys, "stderr", UnwritableStream())
    with pytest.raises(OSError, match=f"fixture {failure} failure"):
        snug._emit_cli_create_warning("warning: fixture")
