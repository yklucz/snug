"""CLI error wording remains useful without changing engine failure contracts."""

import argparse
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

import snug


SCRIPT = Path(__file__).resolve().parents[1] / "snug.py"


def run_cli(*args, no_extras=False):
    startup = ["-S"] if no_extras else []
    return subprocess.run(
        [sys.executable, "-B", *startup, str(SCRIPT), *map(str, args)],
        env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1", NO_COLOR="1"),
        capture_output=True, text=True, timeout=45,
    )


def assert_error(result, code, *details):
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    assert all(detail.lower() in result.stderr.lower() for detail in details), result.stderr
    assert all(detail not in result.stderr for detail in (
        "Traceback", "ZipInfo", "ReadError(", "BrokenPipeError", "[Errno", ".snug-part-",
    ))
    assert "\x1b" not in result.stderr


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "sample.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("first.txt", b"first payload" * 100)
        output.writestr("second.txt", b"second payload" * 100)
    return path


@pytest.mark.parametrize("command", ["extract", "list", "info", "test"])
def test_missing_archive_context(tmp_path, command):
    path = tmp_path / "missing.zip"
    assert_error(run_cli(command, path), 2, str(path), "does not exist", "check the path")


def test_unreadable_archive_context(archive):
    old_mode = archive.stat().st_mode
    try:
        archive.chmod(0)
        assert_error(run_cli("test", archive), 2, str(archive), "permission denied", "permissions")
    finally:
        archive.chmod(old_mode)


def test_create_permission_context(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("payload")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    output = blocked / "output.zip"
    old_mode = blocked.stat().st_mode
    try:
        blocked.chmod(0o500)
        assert_error(run_cli("create", output, source), 2, str(output), "permission denied", "writable")
    finally:
        blocked.chmod(old_mode)
    assert not output.exists()


@pytest.mark.parametrize("command", ["extract", "list", "info", "test"])
def test_truncated_zip_context(archive, command):
    archive.write_bytes(archive.read_bytes()[:-16])
    assert_error(run_cli(command, archive), 2, str(archive), "corrupt or truncated", "complete copy")


@pytest.mark.parametrize("command", ["extract", "list", "info", "test"])
def test_truncated_tar_context(tmp_path, command):
    path = tmp_path / "truncated.tar"
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as output:
        for name in ("first.txt", "second.txt"):
            member = tarfile.TarInfo(name)
            member.size = 5
            output.addfile(member, io.BytesIO(b"hello"))
    path.write_bytes(path.read_bytes()[:1031])
    destination = tmp_path / "output"
    args = [command, path] + (["-C", destination] if command == "extract" else [])
    assert_error(run_cli(*args), 2, str(path), "truncated", "complete copy")
    assert not destination.exists()


def test_corrupt_tar_context(tmp_path):
    path = tmp_path / "corrupt.tar"
    path.write_bytes(b"not a tar header" * 64)
    assert_error(run_cli("test", path), 2, str(path), "invalid header", "complete copy")


@pytest.mark.parametrize("command", ["extract", "test"])
@pytest.mark.parametrize("name", ["payload.txt", "bad password truncated crc.txt"])
def test_crc_error_context(tmp_path, command, name):
    path = tmp_path / "bad-crc.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr(name, b"payload")
    with zipfile.ZipFile(path) as handle:
        member = handle.getinfo(name)
        offset = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
    damaged = bytearray(path.read_bytes())
    damaged[offset] ^= 1
    path.write_bytes(damaged)
    destination = tmp_path / "output"
    args = [command, path] + (["-C", destination] if command == "extract" else [])
    result = run_cli(*args)
    assert_error(result, 2, str(path), name, "integrity", "complete copy")
    assert "incorrect password;" not in result.stderr
    assert not (destination / name).exists()


@pytest.mark.parametrize("option,name", [
    ("--max-file-size", "selected entries.txt"),
    ("--max-ratio", "file limit.txt"),
])
def test_limit_words_in_member_names_do_not_select_recovery_advice(tmp_path, option, name):
    archive = tmp_path / "limits.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(name, b"x" * 1000)
    result = run_cli("extract", archive, "-C", tmp_path / "output", option, "1")
    assert_error(result, 4, str(archive), name, f"adjust {option}")
    for other in {"--max-files", "--max-size", "--max-file-size", "--max-ratio"} - {option}:
        assert f"adjust {other}" not in result.stderr
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("command", ["extract", "test"])
def test_missing_zip_password_context(fixture_dir, tmp_path, command):
    path = fixture_dir / "sample-traditional.zip"
    args = [command, path] + (["-C", tmp_path / "output"] if command == "extract" else [])
    assert_error(run_cli(*args), 2, str(path), "hello.txt", "password", "--password-file")


@pytest.mark.parametrize("command", ["extract", "test"])
def test_wrong_zip_password_context(fixture_dir, tmp_path, command):
    path = fixture_dir / "sample-traditional.zip"
    password = tmp_path / "password.txt"
    password.write_text("incorrect\n")
    args = [command, path, "--password-file", password]
    if command == "extract":
        args += ["-C", tmp_path / "output"]
    result = run_cli(*args)
    assert_error(result, 2, str(path), "hello.txt", "incorrect password", "check the password")
    assert "fixture-password" not in result.stderr


def test_unknown_format_context(tmp_path):
    path = tmp_path / "unknown.blob"
    path.write_text("unknown")
    assert_error(run_cli("test", path), 2, str(path), "cannot determine", "snug formats")


def test_unavailable_optional_format_context(fixture_dir):
    path = fixture_dir / "test_read_format_7zip_lzma1_2.7z"
    assert_error(run_cli("test", path, no_extras=True), 2, str(path), "7z", "install", "snug formats")


@pytest.mark.parametrize("with_password", [False, True])
def test_optional_7z_password_context(tmp_path, py7zr_backend, with_password):
    path = tmp_path / "protected.7z"
    with py7zr_backend.SevenZipFile(path, "w", password="fixture-secret", header_encryption=True) as output:
        output.writestr(b"secret", "secret.txt")
    args = ["test", path]
    if with_password:
        password = tmp_path / "password.txt"
        password.write_text("incorrect\n")
        args += ["--password-file", password]
    result = run_cli(*args)
    assert_error(result, 2, str(path), "password", "check the password" if with_password else "--password-file")
    assert "fixture-secret" not in result.stdout + result.stderr


def test_extract_destination_permission_context(archive, tmp_path):
    destination = tmp_path / "blocked"
    destination.mkdir()
    old_mode = destination.stat().st_mode
    try:
        destination.chmod(0o500)
        assert_error(run_cli("extract", archive, "-C", destination), 2,
                     str(archive), str(destination), "first.txt", "permission denied", "writable")
    finally:
        destination.chmod(old_mode)
    assert list(destination.iterdir()) == []


def test_existing_create_output_context(archive, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("replacement")
    previous = archive.read_bytes()
    assert_error(run_cli("create", archive, source), 2, str(archive), "already exists", "--force")
    assert archive.read_bytes() == previous


@pytest.mark.parametrize("option,value", [
    ("--max-files", "1"), ("--max-size", "1"),
    ("--max-file-size", "1"), ("--max-ratio", "1"),
])
def test_limit_context(archive, tmp_path, option, value):
    destination = tmp_path / "output"
    assert_error(run_cli("extract", archive, "-C", destination, option, value), 4,
                 str(archive), "limit", option, "fewer members")
    assert not destination.exists()


def test_unsafe_member_context(tmp_path):
    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as output:
        output.writestr("../outside.txt", b"unsafe")
    destination = tmp_path / "output"
    assert_error(run_cli("extract", path, "-C", destination), 3,
                 str(path), "unsafe archive", "../outside.txt", "path traversal", "safe member")
    assert not destination.exists()
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize("missing", [False, True])
def test_password_file_context(archive, tmp_path, missing):
    password = tmp_path / "password.txt"
    if not missing:
        password.write_text("line one\nline two\n")
    result = run_cli("test", archive, "--password-file", password)
    assert_error(result, 2, str(archive), str(password),
                 "does not exist" if missing else "exactly one line")


@pytest.mark.parametrize("command", ["doctor", "formats"])
def test_json_report_and_plain_parser_error_boundary(command):
    report = run_cli(command, "--json")
    assert json.loads(report.stdout)["schema_version"] == 1
    assert report.stderr == ""
    invalid = run_cli(command, "--json", "--invalid")
    assert invalid.returncode == 2
    assert invalid.stdout == ""
    assert "error:" in invalid.stderr and "unrecognized arguments" in invalid.stderr


@pytest.mark.parametrize("number,reason,advice", [
    (errno.ENOSPC, "no space left", "free space"),
    (errno.ENAMETOOLONG, "too long", "shorter"),
])
def test_output_os_error_classification_without_disk_fault(tmp_path, number, reason, advice):
    args = argparse.Namespace(command="extract", archive=tmp_path / "archive.zip",
                              directory=tmp_path / "output", password_file=None)
    cause = OSError(number, os.strerror(number), str(tmp_path / "output/.snug-part-random"))
    error = snug.ArchiveError(str(cause))
    error.__cause__ = cause
    message = snug._cli_error_text(args, error, member="payload.txt")
    assert all(text in message for text in (str(args.archive), str(args.directory), "payload.txt", reason, advice))
    assert ".snug-part-" not in message and "[Errno" not in message


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("number", [errno.EACCES, errno.ENOSPC])
def test_wrapped_create_output_error_keeps_publication_facts(tmp_path, existing, number):
    args = argparse.Namespace(command="create", archive=tmp_path / "archive.zip",
                              sources=[tmp_path / "source.txt"], password_file=None)
    cause = OSError(number, os.strerror(number), str(tmp_path))
    report = snug.CreateReport(Path(args.archive), snug.ArchiveFormat.ZIP)
    error = snug.CreateError(str(cause), report, preserved_existing=existing)
    error.__cause__ = cause
    message = snug._cli_error_text(args, error)
    assert str(args.archive) in message
    assert "the existing archive was preserved" in message if existing else "no archive was created" in message
    assert "permission denied; choose a writable destination" in message if number == errno.EACCES else "free space" in message
    assert ", file " not in message and "[Errno" not in message


@pytest.mark.parametrize("name", ["crc", "truncated", "bad password", "checksum mismatch"])
@pytest.mark.parametrize("failure", ["missing", "existing", "unknown-format"])
def test_error_words_in_archive_names_do_not_select_the_cause(tmp_path, name, failure):
    source = tmp_path / "source.txt"
    source.write_text("payload")
    path = tmp_path / (name + (".blob" if failure == "unknown-format" else ".zip"))
    if failure == "existing":
        path.write_text("existing archive")
        command = ("create", path, source)
        expected = ("already exists", "--force")
    else:
        if failure == "unknown-format":
            path.write_text("not an archive")
        command = ("test", path)
        expected = (("does not exist", "check the path") if failure == "missing"
                    else ("cannot determine", "snug formats"))
    result = run_cli(*command)
    assert_error(result, 2, str(path), *expected)
    assert "failed its integrity check" not in result.stderr
    assert "archive is truncated;" not in result.stderr
    assert "incorrect password;" not in result.stderr
