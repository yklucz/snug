"""CLI progress must not obscure failures or abort work on a closed pipe."""

import io
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

import snug


SCRIPT = Path(__file__).resolve().parents[1] / "snug.py"


class _Output(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


@pytest.mark.parametrize("command", ["create", "extract", "test"])
@pytest.mark.parametrize("interrupt", [False, True], ids=["error", "Ctrl-C"])
@pytest.mark.parametrize("tty", [False, True], ids=["pipe", "TTY"])
@pytest.mark.parametrize("quiet", [False, True], ids=["normal", "quiet"])
def test_cli_failure_terminates_only_an_active_tty_progress_line(
        tmp_path, monkeypatch, command, interrupt, tty, quiet):
    output, errors = io.StringIO(), _Output(tty)
    monkeypatch.setattr(snug.sys, "stdout", output)
    monkeypatch.setattr(snug.sys, "stderr", errors)
    monkeypatch.setattr(snug, "_start_update_check", lambda: None)

    def fail(*args, progress, **kwargs):
        progress.start(100, 3)
        progress.item("payload.txt")
        progress.chunk(10)
        raise KeyboardInterrupt() if interrupt else snug.ArchiveError("injected read failure")

    engine = SimpleNamespace(**{name: fail for name in ("create", "extract", "test")})
    monkeypatch.setattr(snug, "ArchiveEngine", lambda: engine)
    arguments = [command, str(tmp_path / "archive.zip")]
    if command == "create":
        arguments.append(str(tmp_path / "source.txt"))
    if quiet:
        arguments.append("-q")

    assert snug.main(arguments) == (130 if interrupt else 2)
    rendered = errors.getvalue()
    assert output.getvalue() == ""
    assert "Archive is OK." not in rendered
    assert "Traceback" not in rendered
    if tty and not quiet:
        first_line, remaining = rendered.split("\n", 1)
        assert "\r" in first_line and command in first_line
        assert "error:" not in first_line and "interrupted" not in first_line
        assert ("interrupted" if interrupt else "error:") in remaining
    else:
        assert "\r" not in rendered and "\x1b" not in rendered
        assert f"{command}: 1/3 items" not in rendered


def test_create_skip_warnings_follow_the_terminated_progress_line(tmp_path, monkeypatch):
    errors = _Output(True)
    monkeypatch.setattr(snug.sys, "stdout", io.StringIO())
    monkeypatch.setattr(snug.sys, "stderr", errors)
    monkeypatch.setattr(snug, "_start_update_check", lambda: None)
    report = snug.CreateReport(tmp_path / "archive.zip", snug.ArchiveFormat.ZIP)
    report.record_skip(tmp_path / "omitted.txt", "permission denied")

    def fail(*args, progress, **kwargs):
        progress.start(100, 2)
        raise snug.CreateError("archive was not created", report)

    monkeypatch.setattr(snug, "ArchiveEngine", lambda: SimpleNamespace(create=fail))
    assert snug.main(["create", str(report.archive), str(tmp_path / "source.txt")]) == 2
    first_line, remaining = errors.getvalue().split("\n", 1)
    assert "\r" in first_line
    assert remaining.startswith("warning: skipped ")
    assert "\nerror: " in remaining


@pytest.mark.parametrize("tty,quiet", [(True, False), (False, False), (True, True)])
def test_success_does_not_gain_an_extra_abort_line(tty, quiet):
    output = _Output(tty)
    display = snug._CliProgressDisplay("test", quiet=quiet, stream=output)
    display.start(100, 1)
    display.item("payload.txt")
    display.chunk(100)
    display.done()
    completed = output.getvalue()
    display.abort()
    display.abort()
    assert output.getvalue() == completed
    if quiet:
        assert completed == ""
    else:
        assert completed.endswith("\n")
        if not tty:
            assert "\x1b" not in completed


def test_abort_is_idempotent_and_does_not_claim_completion():
    output = _Output(True)
    display = snug._CliProgressDisplay("extract", stream=output)
    display.start(100, 2)
    unfinished = output.getvalue()
    display.abort()
    display.abort()
    assert output.getvalue() == unfinished + "\n"
    assert "100.0%" not in output.getvalue()


@pytest.mark.parametrize("tty", [False, True])
@pytest.mark.parametrize("failure", ["write", "flush"])
def test_closed_progress_pipe_disables_future_rendering(monkeypatch, tty, failure):
    class ClosedOutput(_Output):
        def write(self, text):
            if failure == "write":
                raise BrokenPipeError("closed consumer")
            return super().write(text)

        def flush(self):
            if failure == "flush":
                raise BrokenPipeError("closed consumer")
            return super().flush()

    output = ClosedOutput(tty)
    silenced = []
    monkeypatch.setattr(snug, "_silence_closed_pipe", silenced.append)
    display = snug._CliProgressDisplay("extract", stream=output)
    display.start(100, 1)
    display.item("payload.txt")
    display.chunk(100)
    display.done()
    assert display.quiet and silenced == [output]
    previous = output.getvalue()
    display.chunk(10)
    display.done()
    display.abort()
    assert output.getvalue() == previous


def test_progress_pipe_closing_during_abort_does_not_replace_the_failure(monkeypatch):
    class ClosingOutput(_Output):
        closed_consumer = False

        def flush(self):
            if self.closed_consumer:
                raise BrokenPipeError("consumer closed after progress began")
            return super().flush()

    output = ClosingOutput(True)
    silenced = []
    monkeypatch.setattr(snug, "_silence_closed_pipe", silenced.append)
    display = snug._CliProgressDisplay("extract", stream=output)
    display.start(100, 1)
    output.closed_consumer = True
    failure = snug.ArchiveError("original extraction failure")
    with pytest.raises(snug.ArchiveError) as raised:
        try:
            raise failure
        finally:
            display.abort()
    assert raised.value is failure
    assert display.quiet and silenced == [output]


@pytest.mark.parametrize("command", ["create", "extract", "test"])
def test_real_cli_closed_stderr_preserves_success_and_disk_effects(tmp_path, command):
    payload = b"closed stderr must not interrupt archive work\n" * 1024
    source = tmp_path / "payload.txt"
    source.write_bytes(payload)
    archive = tmp_path / "archive.zip"
    destination = tmp_path / "output"
    if command != "create":
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.writestr(source.name, payload)
    arguments = [command, str(archive)]
    if command == "create":
        arguments.append(str(source))
    elif command == "extract":
        arguments.extend(["-C", str(destination)])

    with subprocess.Popen(
            [sys.executable, "-B", str(SCRIPT), *arguments],
            cwd=tmp_path, env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        assert process.stderr is not None
        process.stderr.close()
        process.stderr = None
        output, _ = process.communicate(timeout=30)
        assert process.returncode == 0
    expected = {"create": "Created", "extract": "Extracted", "test": "Archive is OK."}
    assert expected[command] in output
    if command == "create":
        with zipfile.ZipFile(archive) as handle:
            assert handle.testzip() is None
            assert handle.read(source.name) == payload
    elif command == "extract":
        assert (destination / source.name).read_bytes() == payload
