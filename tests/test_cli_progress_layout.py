"""CLI progress remains honest and bounded by its own output terminal."""

import bz2
import errno
import gzip
import io
import lzma
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time
import zipfile

import pytest

import snug
import snug_core as core


SCRIPT = Path(__file__).resolve().parents[1] / "snug.py"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class TerminalOutput(io.StringIO):
    def __init__(self, tty=True, descriptor=73):
        super().__init__()
        self.tty = tty
        self.descriptor = descriptor

    def isatty(self):
        return self.tty

    def fileno(self):
        return self.descriptor


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(core.time, "monotonic", clock)
    return clock


def frames(output):
    """Return visible progress frames without terminal-control sequences."""
    return [ANSI.sub("", frame).rstrip("\n")
            for frame in output.split("\r") if frame.strip("\n")]


def fixed_width(monkeypatch, columns, descriptor=73):
    requested = []

    def terminal_size(actual):
        requested.append(actual)
        assert actual == descriptor
        return os.terminal_size((columns, 24))

    monkeypatch.setattr(core.os, "get_terminal_size", terminal_size)
    return requested


def finish(display, skipped=0):
    display.done()
    display.finish(skipped_count=skipped)


@pytest.mark.parametrize("columns", [20, 30, 40, 60, 80])
def test_cli_progress_uses_output_descriptor_with_stdout_redirected(
        monkeypatch, clock, columns):
    output = TerminalOutput()
    monkeypatch.setattr(snug.sys, "stdout", io.StringIO())
    monkeypatch.setenv("COLUMNS", "999")
    requested = fixed_width(monkeypatch, columns)
    display = snug._CliProgressDisplay("extract", stream=output, defer_final=True)
    display.start(100, 2)
    display.item("界e\u0301" * 100)
    clock.now += 0.101
    display.chunk(50)
    finish(display)
    rendered = frames(output.getvalue())
    assert requested and set(requested) == {output.fileno()}
    assert len(rendered) >= 3
    assert all(core._cell_width(line) <= columns - 1 for line in rendered)
    assert "50" in rendered[-2] and "%" in rendered[-2]
    assert re.search(r"100(?:\.0)?%", rendered[-1])
    assert "/" in rendered[-1], "a progress counter must survive compact layout"


@pytest.mark.parametrize("failure", [OSError("unavailable"), ValueError("closed")])
def test_cli_progress_has_a_bounded_fallback_width(monkeypatch, clock, failure):
    output = TerminalOutput()

    def unavailable(descriptor):
        raise failure

    monkeypatch.setattr(core.os, "get_terminal_size", unavailable)
    display = snug._CliProgressDisplay("create", stream=output, defer_final=True)
    display.start(100, 1)
    display.item("界" * 200)
    display.chunk(100)
    finish(display)
    assert all(core._cell_width(line) <= 79 for line in frames(output.getvalue()))
    assert re.search(r"100(?:\.0)?%", frames(output.getvalue())[-1])


@pytest.mark.parametrize("text,expected", [
    ("界", 2), ("Ａ", 2), ("e\u0301", 1), ("é", 1),
    ("Å", 1), ("A\u030a", 1), ("\x1b[31m界e\u0301\x1b[0m", 3),
])
def test_progress_cell_width_ignores_combining_marks_and_sgr(text, expected):
    assert core._cell_width(text) == expected


@pytest.mark.parametrize("text,width,expected", [
    ("界ab", 3, "界…"), ("界ab", 2, "…"),
    ("e\u0301xyz", 2, "e\u0301…"), ("éxyz", 2, "é…"),
    ("A\u030axyz", 2, "A\u030a…"), ("Åxyz", 2, "Å…"),
    ("界ab", 0, ""), ("界ab", 4, "界ab"),
])
def test_progress_truncation_preserves_whole_wide_characters_and_normalization(
        text, width, expected):
    assert core._truncate_cells(text, width) == expected
    assert core._cell_width(core._truncate_cells(text, width)) <= width


def test_progress_truncation_keeps_sgr_balanced():
    text = "\x1b[31m界abc\x1b[0m"
    shortened = core._truncate_cells(text, 3)
    assert ANSI.sub("", shortened) == "界…"
    assert shortened.startswith("\x1b[31m") and shortened.endswith("\x1b[0m")


@pytest.mark.parametrize("columns", [20, 30, 40, 60, 80])
def test_large_progress_numbers_keep_a_counter_without_wrapping(monkeypatch, clock, columns):
    output = TerminalOutput()
    fixed_width(monkeypatch, columns)
    display = snug._CliProgressDisplay("extract", stream=output, defer_final=True)
    display.start(10 ** 42, 10 ** 30)
    display.items_done = 10 ** 29
    clock.now += 0.101
    display.chunk(10 ** 41)
    finish(display)
    rendered = frames(output.getvalue())
    assert all(core._cell_width(line) <= columns - 1 for line in rendered)
    assert "%" in rendered[-2] and "%" in rendered[-1]
    assert "/" in rendered[-1].replace("/s", "")


def test_progress_throttle_is_100_ms_and_final_render_is_forced(monkeypatch, clock):
    output = TerminalOutput()
    fixed_width(monkeypatch, 80)
    display = snug._CliProgressDisplay("create", stream=output, defer_final=True)
    display.start(100, 1)
    initial = output.getvalue()
    display.item("payload.txt")
    clock.now += 0.099
    display.chunk(1)
    assert output.getvalue() == initial
    clock.now += 0.002
    display.chunk(1)
    assert len(frames(output.getvalue())) == 2
    finish(display)
    assert len(frames(output.getvalue())) == 3


def test_every_repaint_erases_old_text_including_after_terminal_shrink(monkeypatch, clock):
    output = TerminalOutput()
    columns = [120]
    monkeypatch.setattr(core.os, "get_terminal_size",
                        lambda descriptor: os.terminal_size((columns[0], 24)))
    display = snug._CliProgressDisplay("extract", stream=output, defer_final=True)
    display.start(100, 2)
    clock.now += 0.101
    display.item("long-name-" * 100)
    before = output.getvalue()
    columns[0] = 20
    clock.now += 0.101
    display.item("x")
    repaint = output.getvalue()[len(before):]
    assert repaint.startswith("\r") and "\x1b[K" in repaint
    assert core._cell_width(ANSI.sub("", repaint).strip("\r\n")) <= 19
    assert all("\x1b[K" in frame for frame in output.getvalue().split("\r") if frame)


@pytest.mark.parametrize("total_items", [0, 1, 3])
def test_unknown_total_is_indeterminate_until_confirmed_completion(monkeypatch, clock, total_items):
    output = TerminalOutput()
    fixed_width(monkeypatch, 80)
    display = snug._CliProgressDisplay("test", stream=output, defer_final=True)
    display.start(0, total_items)
    if total_items:
        display.item("payload")
    clock.now += 0.101
    display.chunk(100)
    live = frames(output.getvalue())
    assert all("%" not in frame and "█" not in frame and "░" not in frame for frame in live)
    assert "100" in live[-1] and "/s" in live[-1]
    assert "elapsed" in live[-1]
    before_done = output.getvalue()
    display.done()
    assert output.getvalue() == before_done, "engine completion waits for the returned report"
    display.finish()
    assert re.search(r"\b(done|complete|completed|finished)\b", frames(output.getvalue())[-1], re.I)
    assert display.bytes_done == 100 and display.total_bytes == 0


@pytest.mark.parametrize("total_items", [1, 3])
def test_item_only_create_keeps_its_counter_without_claiming_byte_measurements(
        monkeypatch, clock, total_items):
    output = TerminalOutput()
    fixed_width(monkeypatch, 80)
    display = snug._CliProgressDisplay("create", stream=output, defer_final=True)
    display.start(0, total_items)
    initial = frames(output.getvalue())[-1]
    assert "working" in initial and f"0/{total_items}" in initial
    for number in range(total_items):
        clock.now += 0.101
        display.item(f"file{number}")
    finish(display)
    rendered = frames(output.getvalue())
    assert "done" in rendered[-1] and f"{total_items}/{total_items}" in rendered[-1]
    assert all("%" not in frame and "█" not in frame and "░" not in frame
               and not re.search(r"\[[=-]+\]", frame) for frame in rendered)
    assert all("/s" not in frame and not re.search(r"\b\d+(?:\.\d+)?\s*[KMGTPE]?B\b", frame)
               for frame in rendered)


def test_all_skipped_work_finishes_complete_without_fabricating_bytes(monkeypatch, clock):
    output = TerminalOutput()
    fixed_width(monkeypatch, 80)
    display = snug._CliProgressDisplay("extract", stream=output, defer_final=True)
    display.start(100, 2)
    display.item("first.txt")
    display.item("second.txt")
    finish(display, skipped=2)
    final = frames(output.getvalue())[-1]
    assert re.search(r"100(?:\.0)?%", final)
    assert re.search(r"(?:skip(?:ped)?\s*[:=]?\s*2|2\s*skip(?:ped)?)", final, re.I)
    assert display.bytes_done == 0 and display.total_bytes == 100
    assert all("skip" not in frame.lower() for frame in frames(output.getvalue())[:-1])


def test_zero_byte_skipped_work_reports_completion_and_skips(monkeypatch, clock):
    output = TerminalOutput()
    fixed_width(monkeypatch, 80)
    display = snug._CliProgressDisplay("extract", stream=output, defer_final=True)
    display.start(0, 2)
    display.item("empty-one")
    display.item("empty-two")
    finish(display, skipped=2)
    final = frames(output.getvalue())[-1]
    assert "done" in final and "%" not in final
    assert re.search(r"skip(?:ped)?\s+2", final)
    assert display.bytes_done == 0


@pytest.mark.parametrize("total_bytes", [0, 100])
def test_huge_final_skip_count_stays_readable_at_20_columns(monkeypatch, clock, total_bytes):
    output = TerminalOutput()
    fixed_width(monkeypatch, 20)
    display = snug._CliProgressDisplay("extract", stream=output, defer_final=True)
    display.start(total_bytes, 10 ** 30)
    finish(display, skipped=10 ** 30)
    final = frames(output.getvalue())[-1]
    assert core._cell_width(final) <= 19
    assert "…" not in final
    assert re.search(r"skip(?:ped)?\s+1e30", final)
    assert "100%" in final if total_bytes else "done" in final
    assert display.bytes_done == 0


@pytest.mark.parametrize("known_total", [False, True])
def test_non_tty_keeps_the_existing_final_only_format(monkeypatch, clock, known_total):
    output = TerminalOutput(tty=False)
    display = snug._CliProgressDisplay("test", stream=output, defer_final=True)
    display.start(100 if known_total else 0, 1)
    display.item("payload")
    display.chunk(100)
    assert output.getvalue() == ""
    clock.now += 1.0
    finish(display)
    expected = "test: 1/1 items"
    expected += ", 100 B in 00:01 (avg 100 B/s)\n" if known_total else " in 00:01\n"
    assert output.getvalue() == expected
    assert "\r" not in output.getvalue() and "\x1b" not in output.getvalue()


@pytest.mark.parametrize("known_total", [False, True])
def test_non_tty_skips_only_extend_the_final_line(monkeypatch, clock, known_total):
    output = TerminalOutput(tty=False)
    display = snug._CliProgressDisplay("extract", stream=output, defer_final=True)
    display.start(100 if known_total else 0, 2)
    display.item("empty-one")
    display.item("empty-two")
    clock.now += 1.0
    display.done()
    assert output.getvalue() == ""
    display.finish(skipped_count=2)
    final = output.getvalue()
    assert final.startswith("extract: 2/2 items")
    assert final.endswith(", 2 skipped\n") and final.count("\n") == 1
    assert "\r" not in final and "\x1b" not in final


@pytest.mark.parametrize("tty", [False, True])
def test_quiet_progress_stays_silent_through_completion_and_abort(monkeypatch, clock, tty):
    output = TerminalOutput(tty=tty)
    fixed_width(monkeypatch, 80)
    display = snug._CliProgressDisplay("test", quiet=True, stream=output, defer_final=True)
    display.start(100, 1)
    display.item("payload")
    display.chunk(100)
    finish(display, skipped=1)
    display.abort()
    assert output.getvalue() == ""


def run_with_stderr_pty(arguments, columns=80):
    """Run a bounded real command with piped stdout and a sized stderr PTY."""
    import fcntl
    import struct
    import termios

    master, slave = os.openpty()
    child = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
        environment = dict(os.environ, SNUG_NO_UPDATE_CHECK="1", NO_COLOR="1",
                           PYTHONDONTWRITEBYTECODE="1")
        environment.pop("COLUMNS", None)
        environment.pop("LINES", None)
        child = subprocess.Popen([sys.executable, str(SCRIPT), *arguments],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=slave, env=environment)
        os.close(slave)
        slave = -1
        captured = bytearray()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.1)[0]:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            captured.extend(chunk)
        else:
            pytest.fail("CLI progress PTY probe timed out")
        stdout, _ = child.communicate(timeout=5)
        return child.returncode, stdout.decode(), captured.decode()
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()
        os.close(master)
        if slave >= 0:
            os.close(slave)


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="POSIX PTY required")
@pytest.mark.parametrize("suffix,compress", [("gz", gzip.compress), ("bz2", bz2.compress),
                                           ("xz", lzma.compress)])
@pytest.mark.parametrize("empty", [False, True], ids=["payload", "empty"])
def test_real_unknown_compression_streams_never_claim_live_completion(tmp_path, suffix, compress, empty):
    payload = b"" if empty else bytes(range(256)) * 4096
    archive = tmp_path / f"payload.{suffix}"
    archive.write_bytes(compress(payload))
    status, stdout, stderr = run_with_stderr_pty(["test", str(archive)])
    assert status == 0 and "Archive is OK." in stdout
    rendered = frames(stderr)
    assert len(rendered) >= 2
    assert all("%" not in frame and "█" not in frame and "░" not in frame
               for frame in rendered[:-1])
    assert re.search(r"\b(done|complete|completed|finished)\b", rendered[-1], re.I)
    assert all(core._cell_width(frame) <= 79 for frame in rendered)
    assert "Traceback" not in stderr


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="POSIX PTY required")
def test_real_no_overwrite_finishes_complete_with_all_members_skipped(tmp_path):
    archive = tmp_path / "payload.zip"
    destination = tmp_path / "output"
    destination.mkdir()
    names = ["first.txt", "second.txt"]
    with zipfile.ZipFile(archive, "w") as writer:
        for name in names:
            writer.writestr(name, b"archive payload")
            (destination / name).write_bytes(b"preserved destination")
    status, stdout, stderr = run_with_stderr_pty(
        ["extract", str(archive), "-C", str(destination), "--no-overwrite"])
    assert status == 0 and "Extracted 0 items" in stdout
    progress = [frame for frame in frames(stderr) if "100" in frame and "%" in frame]
    assert progress and re.search(r"skip(?:ped)?\s+2", progress[-1])
    assert all((destination / name).read_bytes() == b"preserved destination" for name in names)

