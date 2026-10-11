"""Keep stream and signal ownership correct on every context-manager exit."""

import io
from types import SimpleNamespace

import pytest

import snug


class _TtyOutput(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("outcome", ["normal", "error", "interrupt", "close"])
def test_result_capture_restores_streams_and_commits_only_completed_output(monkeypatch, outcome):
    stdout, stderr = _TtyOutput(), _TtyOutput()
    previous_lines = ["previous result"]
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(snug.sys, "stdout", stdout)
    monkeypatch.setattr(snug.sys, "stderr", stderr)
    monkeypatch.setattr(snug, "_TUI_LAST_LINES", previous_lines)

    def write_result():
        assert snug.sys.stdout is snug.sys.stderr
        assert snug.sys.stdout is not stdout
        print("new result")
        print("new warning", file=snug.sys.stderr)

    context = snug._menu_result_output()
    if outcome == "close":
        context.__enter__()
        write_result()
        context.gen.close()
    else:
        failure = RuntimeError("result failed") if outcome == "error" else KeyboardInterrupt()
        if outcome == "normal":
            with context:
                write_result()
        else:
            with pytest.raises(type(failure)) as raised:
                with context:
                    write_result()
                    raise failure
            assert raised.value is failure

    assert snug.sys.stdout is stdout
    assert snug.sys.stderr is stderr
    assert snug._TUI_LAST_LINES is previous_lines
    assert previous_lines == (["new result", "new warning"] if outcome == "normal"
                              else ["previous result"])
    assert stdout.getvalue() == stderr.getvalue() == ""


@pytest.fixture
def prompt_handlers(monkeypatch):
    delivered = []

    def previous(signum, frame):
        delivered.append((signum, frame))

    numbers = [getattr(snug.signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT")
               if hasattr(snug.signal, name)]
    handlers = dict.fromkeys(numbers, previous)

    def install(signum, handler):
        old = handlers[signum]
        handlers[signum] = handler
        return old

    monkeypatch.setattr(snug.signal, "getsignal", handlers.__getitem__)
    monkeypatch.setattr(snug.signal, "signal", install)
    return SimpleNamespace(handlers=handlers, previous=previous, delivered=delivered)


@pytest.mark.parametrize("outcome", ["normal", "error", "interrupt", "close"])
def test_prompt_signal_context_restores_handler_identity_on_every_exit(prompt_handlers, outcome):
    context = snug._prompt_signals()

    def check_installed():
        assert prompt_handlers.handlers
        assert all(handler is not prompt_handlers.previous
                   for handler in prompt_handlers.handlers.values())

    if outcome == "close":
        context.__enter__()
        check_installed()
        context.gen.close()
    else:
        failure = RuntimeError("prompt failed") if outcome == "error" else KeyboardInterrupt()
        if outcome == "normal":
            with context:
                check_installed()
        else:
            with pytest.raises(type(failure)) as raised:
                with context:
                    check_installed()
                    raise failure
            assert raised.value is failure

    assert all(handler is prompt_handlers.previous for handler in prompt_handlers.handlers.values())
    assert prompt_handlers.delivered == []


def test_prompt_signal_control_flow_bypasses_ordinary_exception_handlers(prompt_handlers):
    signum = snug.signal.SIGTERM
    caught = []
    continued = []
    with snug._prompt_signals():
        try:
            prompt_handlers.handlers[signum](signum, None)
        except Exception:
            caught.append(True)
        continued.append(True)

    assert caught == continued == []
    assert prompt_handlers.delivered == [(signum, None)]
    assert all(handler is prompt_handlers.previous for handler in prompt_handlers.handlers.values())


def test_terminal_eof_bypasses_ordinary_exception_handlers():
    eof = snug._TerminalEOF()
    with pytest.raises(KeyboardInterrupt) as raised:
        try:
            raise eof
        except Exception:
            pytest.fail("ordinary exception handling swallowed terminal EOF")
    assert raised.value is eof
