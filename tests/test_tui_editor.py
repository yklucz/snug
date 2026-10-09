"""Pure line editing and TTY prompt integration, without a real terminal."""

from contextlib import contextmanager, nullcontext
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import snug
from test_tui_input import unix_input
from test_tui_layout import _cells, _terminal_rows
from test_tui_options import make_zip, quiet_tui, script_menus


class _TTYOutput(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def tty_prompt(monkeypatch):
    """Use real prompt/editing code with deterministic keys and no terminal IO."""
    output = _TTYOutput()
    actual_raw_mode = snug._raw_mode
    actual_prompt_raw_mode = snug._prompt_raw_mode
    actual_prompt_signals = snug._prompt_signals
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(snug.sys, "stdout", output)
    monkeypatch.setattr(snug, "_term_size", lambda: (80, 24))
    monkeypatch.setattr(snug, "_raw_mode", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(snug, "_prompt_raw_mode", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(snug, "_prompt_signals", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(snug, "_PROMPT_HISTORY", {})

    def script(keys):
        # Pytest installs its call-phase capture after fixture setup.
        monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: True))
        monkeypatch.setattr(snug.sys, "stdout", output)
        pending = iter(keys)
        reads = []

        def read(timeout):
            reads.append(timeout)
            value = next(pending)
            if isinstance(value, BaseException):
                raise value
            if callable(value):
                return value(output)
            return value

        monkeypatch.setattr(snug, "_read_key_timeout", read)
        return output, reads

    script.raw_mode = actual_raw_mode
    script.prompt_raw_mode = actual_prompt_raw_mode
    script.signals = actual_prompt_signals
    return script


def _type(editor, text):
    for character in text:
        assert editor.handle("space" if character == " " else character) is None


@pytest.mark.parametrize("text", ["", "abc", "Việt", "Cafe\u0301", "界📦e\u0301"])
def test_initial_buffer_keeps_exact_text_and_cursor_at_end(text):
    editor = snug._LineEditor(initial_text=text)

    assert editor.buffer == text
    assert editor.cursor == len(text)
    assert editor.history_position == 0


@pytest.mark.parametrize("key,cursor,expected_buffer,expected_cursor", [
    ("left", 3, "ab cd", 2),
    ("right", 3, "ab cd", 4),
    ("home", 3, "ab cd", 0),
    ("end", 3, "ab cd", 5),
    ("ctrl_a", 3, "ab cd", 0),
    ("ctrl_e", 3, "ab cd", 5),
    ("backspace", 3, "abcd", 2),
    ("delete", 3, "ab d", 3),
    ("ctrl_d", 3, "ab d", 3),
    ("ctrl_k", 3, "ab ", 3),
    ("ctrl_u", 3, "cd", 0),
    ("ctrl_w", 3, "cd", 0),
    ("space", 3, "ab  cd", 4),
    ("界", 3, "ab 界cd", 4),
])
def test_each_edit_key_changes_only_its_defined_region(
        key, cursor, expected_buffer, expected_cursor):
    editor = snug._LineEditor(initial_text="ab cd")
    editor.cursor = cursor

    assert editor.handle(key) is None
    assert editor.buffer == expected_buffer
    assert editor.cursor == expected_cursor


@pytest.mark.parametrize("key,cursor", [
    ("left", 0), ("right", 3), ("backspace", 0),
    ("delete", 3), ("ctrl_d", 3), ("ctrl_u", 0),
    ("ctrl_k", 3), ("ctrl_w", 0),
])
def test_edit_keys_at_buffer_ends_leave_text_and_cursor_valid(key, cursor):
    editor = snug._LineEditor(initial_text="abc")
    editor.cursor = cursor

    assert editor.handle(key) is None
    assert (editor.buffer, editor.cursor) == ("abc", cursor)


@pytest.mark.parametrize("key,result", [("enter", "submit"), ("esc", "cancel")])
@pytest.mark.parametrize("text", ["", " typed text "])
def test_submit_and_cancel_return_actions_without_inserting_key_names(key, result, text):
    editor = snug._LineEditor(initial_text=text)

    assert editor.handle(key) == result
    assert (editor.buffer, editor.cursor) == (text, len(text))


def test_ctrl_d_on_empty_buffer_returns_eof():
    editor = snug._LineEditor()

    assert editor.handle("ctrl_d") == "eof"
    assert (editor.buffer, editor.cursor) == ("", 0)


@pytest.mark.parametrize("key", ["other", "confirm", "\x00", "\x07", "\x1f", "\u200b"])
def test_unknown_controls_are_ignored_and_never_inserted(key):
    editor = snug._LineEditor(initial_text="text")

    assert editor.handle(key) is None
    assert (editor.buffer, editor.cursor) == ("text", 4)


@pytest.mark.parametrize("word", ["abc", "Việt", "界📦", "e\u0301"])
def test_ctrl_w_removes_previous_word_and_adjacent_spaces_only(word):
    prefix = "keep "
    editor = snug._LineEditor(initial_text=prefix + word + "  suffix")
    editor.cursor = len(prefix + word + "  ")

    assert editor.handle("ctrl_w") is None
    assert editor.buffer == "keep suffix"
    assert editor.cursor == len(prefix)


@pytest.mark.parametrize("unit", ["e\u0301", "a\u0302\u0301", "界\u0301"])
def test_left_right_move_over_base_and_following_combining_marks_as_one_unit(unit):
    editor = snug._LineEditor(initial_text="x" + unit + "y")

    editor.handle("left")
    assert editor.cursor == 1 + len(unit)
    editor.handle("left")
    assert editor.cursor == 1
    editor.handle("right")
    assert editor.cursor == 1 + len(unit)
    editor.handle("right")
    assert editor.cursor == len(editor.buffer)
    assert editor.buffer == "x" + unit + "y"


@pytest.mark.parametrize("key", ["backspace", "delete", "ctrl_d"])
@pytest.mark.parametrize("unit", ["e\u0301", "a\u0302\u0301", "界\u0301"])
def test_deletion_removes_one_whole_base_combining_unit(key, unit):
    editor = snug._LineEditor(initial_text="x" + unit + "y")
    editor.cursor = 1 + len(unit) if key == "backspace" else 1

    assert editor.handle(key) is None
    assert editor.buffer == "xy"
    assert editor.cursor == 1


def test_unicode_typing_and_paste_preserve_exact_codepoints():
    text = " Việt Cafe\u0301 界📦 "
    editor = snug._LineEditor()

    _type(editor, text)

    assert editor.buffer == text
    assert editor.cursor == len(text)


@pytest.mark.parametrize("text", ["👩\u200d💻", "👩\u200d👩\u200d👧\u200d👦", "🇻🇳", "👍🏽"])
def test_complex_unicode_remains_intact_during_bounded_navigation(text):
    editor = snug._LineEditor(initial_text=text)
    positions = [editor.cursor]
    for _ in range(len(text) + 1):
        editor.handle("left")
        positions.append(editor.cursor)
    assert editor.cursor == 0
    for _ in range(len(text) + 1):
        editor.handle("right")
        positions.append(editor.cursor)

    assert editor.buffer == text
    assert editor.cursor == len(text)
    assert all(0 <= position <= len(text) for position in positions)
    assert editor.buffer.encode("utf-8").decode("utf-8") == text


@pytest.mark.parametrize("text", ["👩\u200d💻", "🇻🇳", "👍🏽"])
def test_complex_unicode_deletion_keeps_valid_codepoints_and_reaches_empty(text):
    editor = snug._LineEditor(initial_text=text)
    for _ in range(len(text)):
        if not editor.buffer:
            break
        previous_length = len(editor.buffer)
        assert editor.handle("backspace") is None
        assert len(editor.buffer) < previous_length
        assert text.startswith(editor.buffer)
        assert editor.buffer.encode("utf-8").decode("utf-8") == editor.buffer

    assert (editor.buffer, editor.cursor) == ("", 0)


def test_history_navigation_is_bounded_and_restores_the_draft_cursor():
    history = ["first", "second"]
    editor = snug._LineEditor(initial_text="draft", history=history)
    editor.handle("left")
    editor.handle("left")
    draft_cursor = editor.cursor

    editor.handle("up")
    assert (editor.buffer, editor.cursor, editor.history_position) == ("second", 6, 1)
    editor.handle("up")
    assert (editor.buffer, editor.cursor, editor.history_position) == ("first", 5, 0)
    editor.handle("up")
    assert (editor.buffer, editor.history_position) == ("first", 0)
    editor.handle("down")
    assert (editor.buffer, editor.history_position) == ("second", 1)
    editor.handle("down")
    assert (editor.buffer, editor.cursor, editor.history_position) == ("draft", draft_cursor, 2)
    editor.handle("down")
    assert (editor.buffer, editor.cursor, editor.history_position) == ("draft", draft_cursor, 2)
    assert history == ["first", "second"]


def test_empty_history_does_not_replace_draft():
    editor = snug._LineEditor(initial_text="draft")
    editor.handle("left")
    position = editor.cursor

    editor.handle("up")
    editor.handle("down")

    assert (editor.buffer, editor.cursor, editor.history_position) == ("draft", position, 0)


def test_editor_state_and_view_do_not_access_terminal_or_builtin_input(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("pure editor attempted terminal IO"))
    for name in ("_read_key_timeout", "_raw_mode", "_prompt_raw_mode", "_prompt_signals", "_term_size"):
        monkeypatch.setattr(snug, name, forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    editor = snug._LineEditor(initial_text="界e\u0301", history=("older",))
    editor.handle("left")
    editor.handle("X")
    visible, cursor_cells = snug._editor_view(editor, 5)

    assert editor.buffer == "界Xe\u0301"
    assert editor.cursor == 2
    assert visible == editor.buffer
    assert cursor_cells == 3
    forbidden.assert_not_called()


@pytest.mark.parametrize("width", [1, 2, 3, 4, 8, 12])
@pytest.mark.parametrize("text", ["abcdefghi", "界📦界📦", "e\u0301a\u0302界📦" * 4])
def test_horizontal_view_keeps_cursor_visible_and_units_whole(width, text):
    editor = snug._LineEditor(initial_text=text)
    for _ in range(len(text) + 1):
        visible, cursor_cells = snug._editor_view(editor, width)
        assert 0 <= cursor_cells < width
        assert _cells(visible) <= max(0, width - 1)
        assert not visible or visible in text
        assert not visible or not visible[0] in ("\u0301", "\u0302")
        if "e\u0301" in text:
            assert not visible.endswith("e")
        if "a\u0302" in text:
            assert not visible.endswith("a")
        editor.handle("left")
    assert editor.cursor == 0
    visible, cursor_cells = snug._editor_view(editor, width)
    assert cursor_cells == 0
    if width > 2:
        assert visible.startswith(text[:1])


def test_horizontal_view_scrolls_to_the_end_and_back_to_the_start():
    editor = snug._LineEditor(initial_text="0123456789界e\u0301")

    tail, tail_cursor = snug._editor_view(editor, 6)
    assert tail.endswith("界e\u0301")
    assert tail_cursor == _cells(tail)
    assert not tail.startswith("0")
    editor.handle("home")
    head, head_cursor = snug._editor_view(editor, 6)
    assert head.startswith("01234")
    assert head_cursor == 0


@pytest.mark.parametrize("payload,event", [
    (b"\x1b[H", "home"), (b"\x1b[F", "end"),
    (b"\x1bOH", "home"), (b"\x1bOF", "end"),
    (b"\x1b[1~", "home"), (b"\x1b[4~", "end"),
    (b"\x1b[7~", "home"), (b"\x1b[8~", "end"),
    (b"\x1b[3~", "delete"),
    (b"\x01", "ctrl_a"), (b"\x05", "ctrl_e"),
    (b"\x04", "ctrl_d"), (b"\x0b", "ctrl_k"),
    (b"\x15", "ctrl_u"), (b"\x17", "ctrl_w"),
])
def test_editor_sequences_and_controls_decode_to_edit_events(unix_input, payload, event):
    source = unix_input(payload)

    assert snug._read_key_unix() == event
    assert not source.pending


@pytest.mark.parametrize("payload", [
    b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D",
    b"\x1bOA", b"\x1bOB", b"\x1bOC", b"\x1bOD",
    b"\x1b[H", b"\x1b[F", b"\x1bOH", b"\x1bOF", b"\x1b[3~",
    b"\x1b[99~",
])
def test_complete_escape_sequences_never_cancel_the_editor(unix_input, payload):
    source = unix_input(payload)
    editor = snug._LineEditor(initial_text="abc", history=("older",))
    event = snug._read_key_unix()

    assert event != "esc"
    assert editor.handle(event) != "cancel"
    assert not source.pending


def test_only_bare_escape_returns_editor_cancel(unix_input):
    source = unix_input(b"\x1b")
    editor = snug._LineEditor(initial_text="abc")

    assert editor.handle(snug._read_key_unix()) == "cancel"
    assert editor.buffer == "abc"
    assert not source.pending


@pytest.mark.parametrize("prefix", [b"\x1b[", b"\x1b[1;"])
def test_ctrl_c_inside_escape_sequence_keeps_interrupt_semantics(unix_input, prefix):
    source = unix_input(prefix + b"\x03")

    with pytest.raises(KeyboardInterrupt):
        snug._read_key_unix()

    assert not source.pending


def test_tty_prompt_uses_shared_keys_and_never_builtin_input(tty_prompt, monkeypatch):
    output, reads = tty_prompt(["a", "界", "left", "e", "\u0301", "end", "enter"])
    forbidden = Mock(side_effect=AssertionError("TTY prompt called input()"))
    monkeypatch.setattr("builtins.input", forbidden)

    assert snug._prompt("Value") == "ae\u0301界"
    assert reads and all(timeout >= 0 for timeout in reads)
    assert "Value" in output.getvalue() and "Esc" in output.getvalue()
    forbidden.assert_not_called()


@pytest.mark.parametrize("payload,expected", [
    (b"\x1bx\r", "x"),
    (b"a\x1b[DX\r", "Xa"),
    (b"a\x1bODX\r", "Xa"),
    (b"a\x1b[HX\r", "Xa"),
    (b"a\x1bOFX\r", "aX"),
    (b"ab\x1b[D\x1b[3~\r", "a"),
    (b"\x1b[99~x\r", "x"),
])
def test_tty_prompt_does_not_cancel_for_shared_reader_escape_sequences(
        tty_prompt, monkeypatch, unix_input, payload, expected):
    read_key = snug._read_key_timeout
    tty_prompt([])
    source = unix_input(payload)
    snug.sys.stdin.isatty = lambda: True
    monkeypatch.setattr(snug, "_read_key_timeout", read_key)

    assert snug._prompt("Value") == expected
    assert not source.pending


@pytest.mark.parametrize("preserve", [False, True])
def test_tty_prompt_preserves_existing_strip_and_default_contract(tty_prompt, preserve):
    tty_prompt(["space", "V", "space", "enter", "enter"])

    assert snug._prompt("Value", preserve_spaces=preserve) == (" V " if preserve else "V")
    assert snug._prompt("Value", "default", preserve_spaces=preserve) == "default"


def test_tty_prompt_initial_text_is_editable_from_the_end(tty_prompt):
    tty_prompt(["backspace", "X", "enter"])

    assert snug._prompt("Value", initial_text="Cafe\u0301") == "CafX"


def test_tty_ctrl_d_deletes_forward_without_quitting_a_nonempty_buffer(tty_prompt):
    tty_prompt(["home", "ctrl_d", "enter"])

    assert snug._prompt("Value", initial_text="e\u0301界") == "界"


def test_prompt_history_stays_in_memory_and_is_separate_by_kind(tty_prompt, monkeypatch):
    tty_prompt(["a", "enter", "up", "enter", "up", "b", "enter"])
    files = Mock(side_effect=AssertionError("editor history accessed a file"))
    monkeypatch.setattr("builtins.open", files)
    monkeypatch.setattr(Path, "open", files)

    assert snug._prompt("Archive path") == "a"
    assert snug._prompt("Archive path") == "a"
    assert snug._prompt("Destination directory") == "b"
    assert snug._PROMPT_HISTORY["Archive path"][-1] == "a"
    assert snug._PROMPT_HISTORY["Destination directory"][-1] == "b"
    files.assert_not_called()


def test_explicit_history_kind_reuses_history_across_dynamic_labels(tty_prompt):
    tty_prompt(["n", "enter", "up", "enter"])

    assert snug._prompt("first.zip exists", history_key="overwrite") == "n"
    assert snug._prompt("second.zip exists", history_key="overwrite") == "n"
    assert snug._PROMPT_HISTORY["overwrite"][-1] == "n"


def test_escape_discards_prompt_text_and_does_not_record_history_or_quit(tty_prompt):
    tty_prompt(["s", "e", "c", "esc"])

    with pytest.raises(snug._CancelPrompt):
        snug._prompt("Value")

    assert not snug._PROMPT_HISTORY.get("Value")


@pytest.mark.parametrize("event,exception", [("ctrl_d", snug._QuitInteractive),
                                           (KeyboardInterrupt(), snug._CancelPrompt)])
def test_tty_eof_quits_and_ctrl_c_cancels_only_prompt(tty_prompt, event, exception):
    tty_prompt([event])

    with pytest.raises(exception) as raised:
        snug._prompt("Value")

    assert isinstance(raised.value.__cause__, (EOFError, KeyboardInterrupt))
    assert snug._PROMPT_HISTORY == {}


@pytest.mark.parametrize("failure", [RuntimeError("injected"), KeyboardInterrupt()])
def test_prompt_restores_raw_mode_and_signal_context_on_exception(
        tty_prompt, monkeypatch, failure):
    tty_prompt([failure])
    exits = []

    @contextmanager
    def restore(name):
        exits.append((name, "enter"))
        try:
            yield
        finally:
            exits.append((name, "exit"))

    monkeypatch.setattr(snug, "_raw_mode", lambda: restore("raw"))
    monkeypatch.setattr(snug, "_prompt_raw_mode", lambda: restore("raw"))
    monkeypatch.setattr(snug, "_prompt_signals", lambda: restore("signals"))
    expected = snug._CancelPrompt if isinstance(failure, KeyboardInterrupt) else RuntimeError

    with pytest.raises(expected):
        snug._prompt("Value")

    assert sorted(exits) == [("raw", "enter"), ("raw", "exit"),
                            ("signals", "enter"), ("signals", "exit")]


@pytest.mark.parametrize("event,exception", [
    ("enter", None), ("esc", "_CancelPrompt"), ("ctrl_d", "_QuitInteractive"),
    (KeyboardInterrupt(), "_CancelPrompt"), (RuntimeError("injected"), "RuntimeError"),
])
@pytest.mark.parametrize("pending_input", [False, True])
def test_prompt_restores_all_saved_termios_settings_on_every_exit(
        tty_prompt, monkeypatch, event, exception, pending_input):
    termios = pytest.importorskip("termios")
    tty = pytest.importorskip("tty")
    import select
    tty_prompt([event])
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(
        isatty=lambda: True, fileno=lambda: 12345))
    monkeypatch.setattr(snug, "_raw_mode", tty_prompt.raw_mode)
    monkeypatch.setattr(snug, "_prompt_raw_mode", tty_prompt.prompt_raw_mode)
    monkeypatch.setattr(snug.sys, "platform", "darwin")
    saved = [1, 2, 3, 4 | (termios.PENDIN if pending_input else 0),
             5, 6, [b"saved-control-character"]]
    get_settings = Mock(return_value=saved)
    set_raw = Mock()
    lifecycle = []
    restore = Mock(side_effect=lambda *args: lifecycle.append("restore"))
    settle = Mock(side_effect=lambda *args: lifecycle.append("settle") or ([], [], []))
    monkeypatch.setattr(termios, "tcgetattr", get_settings)
    monkeypatch.setattr(termios, "tcsetattr", restore)
    monkeypatch.setattr(tty, "setraw", set_raw)
    monkeypatch.setattr(select, "select", settle)

    if exception is None:
        assert snug._prompt("Value") == ""
    else:
        expected = RuntimeError if exception == "RuntimeError" else getattr(snug, exception)
        with pytest.raises(expected):
            snug._prompt("Value")

    get_settings.assert_called_once_with(12345)
    set_raw.assert_called_once_with(12345, termios.TCSANOW)
    restore.assert_called_once_with(12345, termios.TCSADRAIN, saved)
    assert restore.call_args.args[2] is saved
    if pending_input:
        settle.assert_not_called()
        assert lifecycle == ["restore"]
    else:
        settle.assert_called_once_with([12345], [], [], 0)
        assert lifecycle == ["restore", "settle"]


@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT"])
def test_prompt_signal_restores_raw_mode_before_dispatching_previous_handler(
        tty_prompt, monkeypatch, signal_name):
    tty_prompt([])
    signum = getattr(snug.signal, signal_name)
    restored = []
    delivered = []
    handlers = {}

    def previous(number, frame):
        assert restored == [True]
        delivered.append((number, frame))

    monkeypatch.setattr(snug.signal, "getsignal", lambda number: previous)
    monkeypatch.setattr(snug.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler))
    monkeypatch.setattr(snug, "_prompt_signals", tty_prompt.signals)

    @contextmanager
    def raw(*args, **kwargs):
        try:
            yield
        finally:
            restored.append(True)

    events = iter(["x", "interrupt", "enter"])

    def interrupt(timeout):
        event = next(events)
        if event == "interrupt":
            handlers[signum](signum, None)
            pytest.fail("signal handler did not interrupt prompt")
        return event

    monkeypatch.setattr(snug, "_raw_mode", raw)
    monkeypatch.setattr(snug, "_prompt_raw_mode", raw)
    monkeypatch.setattr(snug, "_read_key_timeout", interrupt)
    assert snug._prompt("Value") == "x"

    assert delivered == [(signum, None)]
    assert restored == [True, True]
    assert handlers and all(handler is previous for handler in handlers.values())
    assert snug._PROMPT_HISTORY == {"Value": ["x"]}


@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT"])
def test_default_prompt_signal_interrupts_after_restoring_raw_and_handlers(
        tty_prompt, monkeypatch, signal_name):
    tty_prompt([])
    signum = getattr(snug.signal, signal_name)
    handlers = {}
    restored = []
    monkeypatch.setattr(snug.signal, "getsignal", lambda number: snug.signal.SIG_DFL)
    monkeypatch.setattr(snug.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler))
    monkeypatch.setattr(snug, "_prompt_signals", tty_prompt.signals)

    @contextmanager
    def raw():
        try:
            yield
        finally:
            restored.append(True)

    def interrupt(timeout):
        handlers[signum](signum, None)
        pytest.fail("signal did not interrupt prompt")

    monkeypatch.setattr(snug, "_prompt_raw_mode", raw)
    monkeypatch.setattr(snug, "_read_key_timeout", interrupt)
    expected = snug._CancelPrompt if signal_name == "SIGINT" else SystemExit

    with pytest.raises(expected) as raised:
        snug._prompt("Value")

    assert restored == [True]
    assert handlers and all(handler == snug.signal.SIG_DFL for handler in handlers.values())
    assert snug._PROMPT_HISTORY == {}
    if signal_name != "SIGINT":
        assert raised.value.code == 128 + signum


def test_signal_installation_failure_restores_handlers_already_installed(tty_prompt, monkeypatch):
    previous = Mock()
    installed = {}
    failed = []

    def install(signum, handler):
        if signum == snug.signal.SIGTERM and handler is not previous and not failed:
            failed.append(True)
            raise RuntimeError("injected signal installation failure")
        installed[signum] = handler

    monkeypatch.setattr(snug.signal, "getsignal", lambda number: previous)
    monkeypatch.setattr(snug.signal, "signal", install)

    with pytest.raises(RuntimeError, match="installation failure"):
        with tty_prompt.signals():
            pytest.fail("failed signal registration entered prompt")

    assert failed == [True]
    assert installed[snug.signal.SIGINT] is previous
    assert all(handler is previous for handler in installed.values())
    previous.assert_not_called()


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
def test_prompt_scrolls_long_unicode_buffer_and_keeps_compact_cancel_hint(
        tty_prompt, monkeypatch, size):
    output, _ = tty_prompt(["enter"])
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    initial = "界📦e\u0301" * 40 + " END"

    assert snug._prompt("Archive path", initial_text=initial, preserve_spaces=True) == initial
    rows = _terminal_rows(output.getvalue(), size)
    visible = "\n".join(rows.values())
    assert "END" in visible
    assert "Esc" in visible
    assert "Cancel" in visible or "cancel" in visible
    assert len(rows) <= size[1]
    assert all(_cells(line) <= size[0] for line in rows.values())


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
def test_prompt_cursor_uses_cells_for_wide_and_combining_text(
        tty_prompt, monkeypatch, size):
    tty_prompt(["left", "left", "enter"])
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    cursor_at = snug._cursor_at
    coordinates = []

    def position(row, col=1, **kwargs):
        coordinates.append((row, col, kwargs.get("size")))
        return cursor_at(row, col, **kwargs)

    monkeypatch.setattr(snug, "_cursor_at", position)
    text = "界e\u0301📦"

    assert snug._prompt("Value", initial_text=text) == text
    # Two Left presses pass the final wide glyph and the complete e+mark unit.
    assert coordinates[-1] == (2, 3 + _cells("界"), size)


def test_idle_resize_repaints_editor_within_new_bounds_and_keeps_text(
        tty_prompt, monkeypatch):
    size = [80, 24]

    def resize(output):
        assert "Enter" in output.getvalue()
        size[:] = (40, 10)
        output.seek(0)
        output.truncate()
        return None

    output, _ = tty_prompt([resize, "enter"])
    monkeypatch.setattr(snug, "_term_size", lambda: tuple(size))
    initial = "界e\u0301" * 40 + " END"

    assert snug._prompt("Value", initial_text=initial) == initial
    rows = _terminal_rows(output.getvalue(), tuple(size))
    assert "END" in "\n".join(rows.values())
    assert "Esc" in "\n".join(rows.values())


@pytest.mark.parametrize("tty_side", ["neither", "stdin", "stdout"])
def test_non_tty_prompt_still_uses_identical_builtin_input_path(
        monkeypatch, capsys, tty_side):
    output = _TTYOutput() if tty_side == "stdout" else io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", output)
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: tty_side == "stdin"))
    read = Mock(return_value="  value  ")
    forbidden = Mock(side_effect=AssertionError("non-TTY prompt entered editor"))
    monkeypatch.setattr("builtins.input", read)
    monkeypatch.setattr(snug, "_read_key_timeout", forbidden)
    monkeypatch.setattr(snug, "_raw_mode", forbidden)

    assert snug._prompt("Value", "default", initial_text="prefill", context=("feedback",)) == "value"
    read.assert_called_once_with("  Value [default]: ")
    forbidden.assert_not_called()


@pytest.mark.parametrize("failure", [EOFError(), KeyboardInterrupt()])
def test_non_tty_prompt_preserves_builtin_eof_and_interrupt_semantics(monkeypatch, failure):
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(snug.sys, "stdout", io.StringIO())
    read = Mock(side_effect=failure)
    monkeypatch.setattr("builtins.input", read)

    with pytest.raises(snug._QuitInteractive) as raised:
        snug._prompt("Value")

    assert raised.value.__cause__ is failure
    read.assert_called_once_with("  Value: ")


def test_non_tty_menu_choice_still_uses_builtin_input_directly(monkeypatch, capsys):
    read = Mock(side_effect=["", "Z", " B "])
    editor = Mock(side_effect=AssertionError("plain menu entered editor"))
    monkeypatch.setattr("builtins.input", read)
    monkeypatch.setattr(snug, "_LineEditor", editor)

    assert snug._select_menu_plain("Choose", [("b", "Back")]) == "b"
    assert read.call_args_list == [(("  Select: ",),)] * 3
    assert "Unknown option" in capsys.readouterr().out
    editor.assert_not_called()


@pytest.mark.parametrize("has_archive", [False, True])
def test_manual_path_escape_returns_without_engine_call(
        tty_prompt, monkeypatch, tmp_path, quiet_tui, has_archive):
    tty_prompt(["x", "esc"])
    if has_archive:
        make_zip(tmp_path / "archive.zip")
    seen = script_menus(monkeypatch, ["m", "b"] if has_archive else [])
    monkeypatch.chdir(tmp_path)
    engine = Mock(spec=snug.ArchiveEngine)

    snug._menu_extract(engine)

    assert engine.mock_calls == []
    assert [title for title, _, _ in seen] == (["Choose an archive"] * 2 if has_archive else [])


def test_manual_retry_prefills_rejected_text_and_keeps_quoted_context(
        tty_prompt, monkeypatch, tmp_path, quiet_tui):
    valid = tmp_path / "valid.zip"
    valid.write_bytes(b"archive")
    monkeypatch.chdir(tmp_path)
    raw = " missing.zip "
    seen = []
    actual_editor = snug._LineEditor

    def editor(*args, **kwargs):
        result = actual_editor(*args, **kwargs)
        seen.append((result.buffer, result.cursor))
        return result

    monkeypatch.setattr(snug, "_LineEditor", editor)
    output, _ = tty_prompt([*("space" if char == " " else char for char in raw),
                            "enter", "ctrl_u", *valid.name, "enter"])

    assert snug._handle_manual_path() == Path(valid.name)
    assert seen == [("", 0), (raw, len(raw))]
    assert f'Rejected: "{raw}"' in output.getvalue()
    assert "Leading spaces: 1" in output.getvalue()
    assert "Trailing spaces: 1" in output.getvalue()


@pytest.mark.parametrize("name", [
    " Việt.zip ", " Vie\u0323\u0302t.zip ", '"quoted"\\literal.zip ',
])
def test_tty_manual_path_keeps_exact_unicode_spaces_and_literal_punctuation(
        tty_prompt, monkeypatch, tmp_path, quiet_tui, name):
    (tmp_path / name).write_bytes(b"archive")
    monkeypatch.chdir(tmp_path)
    tty_prompt([*("space" if char == " " else char for char in name), "enter"])

    selected = snug._handle_manual_path()

    assert selected == Path(name)
    assert selected.read_bytes() == b"archive"


@pytest.mark.parametrize("current", [None, 4])
def test_compression_escape_preserves_prior_value(tty_prompt, quiet_tui, current):
    tty_prompt(["9", "esc"])

    assert snug._menu_compression(current) == current


@pytest.mark.parametrize("size", [(80, 24), (40, 10)])
@pytest.mark.parametrize("kind", ["compression", "strip-components"])
def test_invalid_numeric_prompt_keeps_visible_error_on_retry_then_escape_discards(
        tty_prompt, monkeypatch, tmp_path, quiet_tui, size, kind):
    invalid = "99" if kind == "compression" else "-1"
    message = ("compression level from 0 to 9" if kind == "compression"
               else "count must be a nonnegative integer")
    checked = []

    def retry(output):
        rows = _terminal_rows(output.getvalue(), size)
        assert message in "\n".join(rows.values())
        checked.append(True)
        return "esc"

    tty_prompt([*invalid, "enter", retry])
    monkeypatch.setattr(snug, "_term_size", lambda: size)

    if kind == "compression":
        assert snug._menu_compression(4) == 4
    else:
        options = snug._ExtractOptions(tmp_path / "archive.zip", False, strip_components=2)
        options.select_strip_components()
        assert options.strip_components == 2
    assert checked == [True]


def test_password_file_escape_preserves_existing_password_and_never_reads_secret(
        tty_prompt, monkeypatch, quiet_tui):
    tty_prompt(["p", "esc"])
    script_menus(monkeypatch, ["f"])
    password = Mock(side_effect=AssertionError("cancelled password path was read"))
    monkeypatch.setattr(snug, "_read_password", password)

    assert snug._menu_password("current-secret") == "current-secret"
    password.assert_not_called()
    assert "current-secret" not in str(snug._PROMPT_HISTORY)


def test_output_path_escape_preserves_existing_path_and_writer(
        tty_prompt, tmp_path, quiet_tui):
    tty_prompt(["x", "esc"])
    previous = tmp_path / "archive.zip"
    options = snug._CreateOptions(tmp_path, [], [snug.ArchiveFormat.ZIP],
                                  snug.ArchiveFormat.ZIP, previous)

    options.select_output()

    assert options.archive_path == previous
    assert options.fmt is snug.ArchiveFormat.ZIP


def test_destination_escape_preserves_prior_destination(tty_prompt, tmp_path, quiet_tui):
    tty_prompt(["x", "esc"])
    options = snug._ExtractOptions(tmp_path / "archive.zip", False, destination="prior")

    options.select_destination()

    assert options.destination == "prior"


def test_strip_components_escape_preserves_prior_count(tty_prompt, tmp_path, quiet_tui):
    tty_prompt(["9", "esc"])
    options = snug._ExtractOptions(tmp_path / "archive.zip", False, strip_components=2)

    options.select_strip_components()

    assert options.strip_components == 2


def test_overwrite_escape_is_no_and_keeps_existing_archive(tty_prompt, tmp_path, quiet_tui):
    tty_prompt(["y", "esc"])
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"original")

    assert snug._menu_confirm_overwrite(archive) is False
    assert archive.read_bytes() == b"original"


def test_secure_password_prompt_uses_getpass_and_never_editor_history(monkeypatch):
    getpass = Mock(return_value=" secret ")
    editor = Mock(side_effect=AssertionError("secret entered line editor"))
    monkeypatch.setattr(snug.getpass, "getpass", getpass)
    monkeypatch.setattr(snug, "_LineEditor", editor)
    monkeypatch.setattr(snug, "_PROMPT_HISTORY", {})
    args = SimpleNamespace(password=True, password_file=None)

    assert snug._read_password(args) == " secret "
    getpass.assert_called_once()
    editor.assert_not_called()
    assert snug._PROMPT_HISTORY == {}
