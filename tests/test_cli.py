import os
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "snug.py"


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=cwd,
                          capture_output=True, text=True, timeout=45)


def assert_success(result):
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_cli_version():
    result = run_cli("--version")
    assert_success(result)
    assert result.stdout.startswith("snug ")


@pytest.mark.parametrize("suffix", ["zip", "tar.gz", "gz", "bz2", "xz", "lzma", "7z", "cpio", "ar"])
def test_cli_commands(tmp_path, suffix, request):
    if suffix == "7z":
        request.getfixturevalue("py7zr_backend")
    if suffix in {"cpio", "ar"}:
        request.getfixturevalue("libarchive_backend")
    source = tmp_path / "hello.txt"
    source.write_bytes(b"CLI round trip\n")
    archive = tmp_path / f"hello.txt.{suffix}"
    assert_success(run_cli("create", archive, source, "-q"))
    listed = run_cli("list", archive)
    assert_success(listed)
    assert "hello.txt" in listed.stdout
    info = run_cli("info", archive)
    assert_success(info)
    assert "backend" in info.stdout
    assert "can_extract" in info.stdout
    dest = tmp_path / "output"
    assert_success(run_cli("extract", archive, "-C", dest, "-q"))
    assert (dest / "hello.txt").read_bytes() == source.read_bytes()
    (dest / "hello.txt").write_bytes(b"keep")
    assert_success(run_cli("extract", archive, "-C", dest, "--no-overwrite", "-q"))
    assert (dest / "hello.txt").read_bytes() == b"keep"


@pytest.mark.parametrize("args", [("create",), ("extract",), ("list",), ("info",), ("extract", "missing.zip", "--strip-components", "-1")])
def test_cli_invalid_arguments(args):
    result = run_cli(*args)
    assert result.returncode == 2
    assert "error:" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("command", ["extract", "list", "info"])
def test_cli_missing_archive(command, tmp_path):
    result = run_cli(command, tmp_path / "missing.zip")
    assert result.returncode != 0
    assert "error:" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_create_existing_needs_force(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"new")
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"keep")
    result = run_cli("create", archive, source)
    assert result.returncode != 0
    assert archive.read_bytes() == b"keep"
    assert_success(run_cli("create", archive, source, "--force", "-q"))
    assert zipfile.is_zipfile(archive)


def test_cli_password_file(tmp_path, py7zr_backend):
    password = "test password with spaces"
    archive = tmp_path / "protected.7z"
    with py7zr_backend.SevenZipFile(archive, "w", password=password, header_encryption=True) as handle:
        handle.writestr(b"protected contents", "secret.txt")
    password_file = tmp_path / "password.txt"
    password_file.write_text(password + "\n", encoding="utf-8")
    password_file.chmod(0o600)
    for command in ["list", "info"]:
        result = run_cli(command, archive, "--password-file", password_file)
        assert_success(result)
        assert password not in result.stdout + result.stderr
    dest = tmp_path / "output"
    result = run_cli("extract", archive, "-C", dest, "--password-file", password_file, "-q")
    assert_success(result)
    assert password not in result.stdout + result.stderr
    assert (dest / "secret.txt").read_bytes() == b"protected contents"
    rejected = run_cli("extract", archive, "--password", password)
    assert rejected.returncode == 2
    assert "unrecognized arguments" in rejected.stderr


def test_cli_password_creation_file(tmp_path, py7zr_backend):
    password_file = tmp_path / "password.txt"
    password_file.write_text("create-password\n")
    source = tmp_path / "hello.txt"
    source.write_bytes(b"secret")
    archive = tmp_path / "protected.7z"
    assert_success(run_cli("create", archive, source, "--password-file", password_file, "-q"))
    result = run_cli("extract", archive, "-C", tmp_path / "output", "--password-file", password_file, "-q")
    assert_success(result)
    assert (tmp_path / "output/hello.txt").read_bytes() == b"secret"


@pytest.mark.parametrize("command", ["list", "info", "extract"])
@pytest.mark.parametrize("suffix", ["zip", "tar", "cpio", "7z"])
def test_cli_safe_terminal_filenames(command, tmp_path, suffix, request):
    archive = tmp_path / f"archive.{suffix}"
    malicious_name = "hello\x1b[31mred\x07\rname\n.txt"
    content = b"safe content"
    if suffix == "zip":
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr(malicious_name, content)
    elif suffix == "tar":
        with tarfile.open(archive, "w") as handle:
            info = tarfile.TarInfo(malicious_name)
            info.size = len(content)
            handle.addfile(info, io.BytesIO(content))
    elif suffix == "cpio":
        backend = request.getfixturevalue("libarchive_backend")
        with backend.file_writer(str(archive), "cpio_newc") as handle:
            handle.add_file_from_memory(malicious_name, len(content), content)
    else:
        backend = request.getfixturevalue("py7zr_backend")
        with backend.SevenZipFile(archive, "w") as handle:
            handle.writestr(content, malicious_name)
    args = [command, archive]
    if command == "extract":
        args += ["-C", tmp_path / "output"]
    result = run_cli(*args)
    if os.name == "nt" and command == "extract":
        assert result.returncode in {2, 3}
        assert "error:" in result.stderr
        assert "Traceback" not in result.stderr
    else:
        assert_success(result)
    assert "\x1b" not in result.stdout + result.stderr
    assert "\x07" not in result.stdout + result.stderr
    assert "\r" not in result.stdout + result.stderr
    if command == "list":
        assert len(result.stdout.splitlines()) == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows forbids control characters in physical filenames")
def test_cli_safe_terminal_archive_path(tmp_path):
    archive = tmp_path / "archive\x1b[31m.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("hello.txt", b"safe content")
    result = run_cli("info", archive)
    assert_success(result)
    assert "\x1b" not in result.stdout + result.stderr


def test_cli_unsafe_exit_code(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../evil.txt", b"evil")
    result = run_cli("extract", archive, "-C", tmp_path / "output")
    assert result.returncode == 3
    assert "unsafe archive" in result.stderr
    assert not (tmp_path / "evil.txt").exists()


def test_cli_readonly_fixture(fixture_dir, tmp_path, libarchive_backend):
    archive = fixture_dir / "test_read_format_rar.rar"
    assert_success(run_cli("list", archive))
    assert_success(run_cli("info", archive))
    extra = ["--symlinks", "skip"] if os.name == "nt" else []
    assert_success(run_cli("extract", archive, "-C", tmp_path / "output", "-q", *extra))
    assert (tmp_path / "output/test.txt").read_bytes() == b"test text document\r\n"


def test_cli_oversized_timestamp_does_not_crash(tmp_path):
    archive = tmp_path / "timestamp.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as handle:
        info = tarfile.TarInfo("hello.txt")
        info.size = 5
        info.mtime = 1e100
        handle.addfile(info, io.BytesIO(b"hello"))
    result = run_cli("list", archive, "-v")
    assert_success(result)
    assert "hello.txt" in result.stdout
    assert_success(run_cli("extract", archive, "-C", tmp_path / "output", "-q"))
    assert (tmp_path / "output/hello.txt").read_bytes() == b"hello"
