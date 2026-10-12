"""TUI progress uses the CLI's honest counters within the screen cell budget."""

import io
from types import SimpleNamespace

import pytest

import snug


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def progress_terminal(monkeypatch):
    output = Terminal()
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(snug.sys, "stdout", output)
    monkeypatch.setattr(snug.time, "monotonic", lambda: clock.now)
    return output, clock


def frames(output):
    return [line.split("\x1b[K", 1)[0] for line in output.getvalue().split("\r") if line]


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("items", [0, 1, 3])
def test_unknown_totals_stay_working_until_explicit_completion(
        monkeypatch, progress_terminal, size, items):
    output, clock = progress_terminal
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    display = snug._TuiProgressDisplay("test", stream=output)
    display.start(0, items)
    clock.now += 0.11
    display.item("界é e\u0301 " * 50)
    clock.now += 0.11
    display.chunk(1234)
    before = output.getvalue()
    display.done()
    assert output.getvalue() == before
    assert all("working" in line and "%" not in line and "done" not in line
               for line in frames(output))
    assert "1.21K" in frames(output)[-1]
    display.finish()
    assert "done" in frames(output)[-1]
    assert all(snug._tui_cell_width(line) <= size[0] - 1 for line in frames(output))


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("operation", ["create", "extract"])
def test_skipped_work_finishes_without_fabricating_bytes(
        monkeypatch, progress_terminal, size, operation):
    output, clock = progress_terminal
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    display = snug._TuiProgressDisplay(operation, stream=output)
    display.start(1000, 4)
    display.chunk(50)
    display.finish(3)
    final = frames(output)[-1]
    assert "100" in final and "4/4" in final and "skip 3" in final
    assert display.bytes_done == 50
    assert snug._tui_cell_width(final) <= size[0] - 1
    assert final == snug._cli_progress_line(display, size[0] - 1, 1e-9)


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
def test_item_only_create_omits_unavailable_rate(monkeypatch, progress_terminal, size):
    output, clock = progress_terminal
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    display = snug._TuiProgressDisplay("create", stream=output)
    display.start(0, 3)
    assert "0/3" in frames(output)[-1]
    assert "/s" not in frames(output)[-1]
    display.finish(1)
    assert "done" in frames(output)[-1] and "3/3" in frames(output)[-1]
    assert "skip 1" in frames(output)[-1]
    assert "%" not in output.getvalue()


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("name", ["界🙂" * 50, "é" * 100, "e\u0301" * 100])
def test_unicode_width_resize_and_throttle(monkeypatch, progress_terminal, size, name):
    output, clock = progress_terminal
    current_size = [size]
    monkeypatch.setattr(snug, "_term_size", lambda: current_size[0])
    display = snug._TuiProgressDisplay("extract", stream=output)
    display.start(100, 1)
    before = output.getvalue()
    display.item(name)
    clock.now += 0.099
    display.chunk(10)
    assert output.getvalue() == before
    clock.now += 0.002
    display.chunk(10)
    assert output.getvalue() != before
    assert snug._tui_cell_width(frames(output)[-1]) <= size[0] - 1
    current_size[0] = (40, 10)
    display.finish()
    assert snug._tui_cell_width(frames(output)[-1]) <= 39
    assert "\x1b[K" in output.getvalue()


def test_abort_never_claims_completion(progress_terminal):
    output, clock = progress_terminal
    display = snug._TuiProgressDisplay("test", stream=output)
    display.start(0, 1)
    display.abort()
    assert "done" not in output.getvalue() and "100%" not in output.getvalue()
    assert output.getvalue().endswith("\n")
