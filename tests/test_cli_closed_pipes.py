"""Report consumers may close stdout without changing CLI status or leaking errors."""

import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

import snug


SCRIPT = Path(snug.__file__)


@pytest.fixture(scope="module")
def pipe_archive(tmp_path_factory):
    path = tmp_path_factory.mktemp("cli-pipes") / "many.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(4000):
            archive.writestr(f"{index:04}-{'member-name-' * 8}.txt", b"payload")
    return path


@pytest.mark.parametrize("command,structured", [
    ("list", False), ("info", False), ("formats", False), ("doctor", False),
    ("formats", True), ("doctor", True),
])
@pytest.mark.parametrize("closure", ["buffered", "unbuffered", "first-line"])
def test_real_report_pipe_closure_is_silent_success(pipe_archive, command, structured, closure):
    arguments = [sys.executable]
    if closure == "unbuffered":
        arguments.append("-u")
    arguments += [str(SCRIPT), command]
    if command in {"list", "info"}:
        arguments.append(str(pipe_archive))
    if command == "list":
        arguments.append("--verbose")
    if structured:
        arguments.append("--json")
    reader, writer = os.pipe()
    process = None
    if closure != "first-line":
        os.close(reader)
        reader = None
    try:
        process = subprocess.Popen(arguments, stdout=writer, stderr=subprocess.PIPE,
                                   env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1"))
        os.close(writer)
        writer = None
        if reader is not None:
            with os.fdopen(reader, "rb") as consumer:
                reader = None
                first_line = consumer.readline()
            if not first_line:
                _, error = process.communicate(timeout=30)
                pytest.fail(f"Command produced no first line: {error.decode()}")
        _, error = process.communicate(timeout=30)
        assert process.returncode == 0, error.decode()
        assert error == b""
    finally:
        for descriptor in (reader, writer):
            if descriptor is not None:
                os.close(descriptor)
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


@pytest.mark.parametrize("command", ["doctor", "formats"])
def test_diagnostic_operational_errors_keep_plain_stderr_and_empty_stdout(monkeypatch, capsys, command):
    def fail(_args):
        raise OSError("fixture cannot be read")

    monkeypatch.setattr(snug, f"_cmd_{command}", fail)
    assert snug.main([command, "--json"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "error: fixture cannot be read\n"


@pytest.mark.parametrize("command", ["doctor", "formats"])
def test_diagnostic_argument_errors_keep_empty_stdout(capsys, command):
    with pytest.raises(SystemExit) as error:
        snug.main([command, "--json", "--not-an-option"])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "error:" in output.err


@pytest.mark.parametrize("command", ["doctor", "formats"])
def test_complete_json_report_schema_is_unchanged(command):
    result = subprocess.run([sys.executable, str(SCRIPT), command, "--json"],
                            capture_output=True, text=True, timeout=30,
                            env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1"))
    assert result.returncode == 0
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    expected = ({"schema_version", "ok", "snug_version", "python", "platform",
                 "backends", "runtime", "updater"} if command == "doctor"
                else {"schema_version", "formats", "note"})
    assert set(report) == expected


@pytest.mark.parametrize("command", ["list", "info", "test", "extract"])
def test_closed_error_pipe_preserves_existing_failure_status(tmp_path, command):
    reader, writer = os.pipe()
    os.close(reader)
    try:
        process = subprocess.Popen([sys.executable, "-u", str(SCRIPT), command,
                                    str(tmp_path / "missing.zip")],
                                   stdout=subprocess.PIPE, stderr=writer,
                                   env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1"))
    finally:
        os.close(writer)
    output, _ = process.communicate(timeout=30)
    assert process.returncode == 1
    assert output == b""
