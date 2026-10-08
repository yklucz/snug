"""Exercise Unix key decoding with deterministic byte arrivals, without a TTY."""

from collections import deque
import os
import select
from types import SimpleNamespace

import pytest

import snug


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix terminal key reader")


class _ScriptedInput:
    """Model bytes becoming readable, timeouts, and EOF without waiting."""

    def __init__(self, payload, arrivals=(), *, eof=False):
        self.fd = 12345
        self.pending = bytearray(payload)
        self.arrivals = deque(arrivals)
        self.eof = eof
        self.waits = []

    def read(self, fd, count):
        assert fd == self.fd
        assert count > 0
        if not self.pending and self.arrivals:
            arrival = self.arrivals.popleft()
            assert arrival is not None, "read would block instead of respecting a timeout"
            self.pending.extend(arrival)
            if not arrival:
                self.eof = True
        if not self.pending:
            assert self.eof, "read would block because no bytes have arrived"
            return b""
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result

    def ready(self, readers, writers, errors, timeout):
        assert readers == [self.fd]
        assert writers == errors == []
        assert timeout is not None and timeout >= 0
        self.waits.append(timeout)
        if not self.pending and self.arrivals:
            arrival = self.arrivals.popleft()
            if arrival is None:
                return ([], [], [])
            self.pending.extend(arrival)
            if not arrival:
                self.eof = True
        return ([self.fd] if self.pending or self.eof else [], [], [])


@pytest.fixture
def unix_input(monkeypatch):
    # Isolate the reader's pending bytes between independently scripted inputs.
    monkeypatch.setattr(snug, "_UNIX_KEY_PUSHBACK", deque())

    def script(payload, arrivals=(), *, eof=False):
        source = _ScriptedInput(payload, arrivals, eof=eof)
        monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(fileno=lambda: source.fd))
        monkeypatch.setattr(os, "read", source.read)
        monkeypatch.setattr(select, "select", source.ready)
        return source

    return script


@pytest.mark.parametrize("text", ["Việt", "Tiếng Việt", "éễ📦", "Cafe\u0301"])
@pytest.mark.parametrize("mode", ["typed", "pasted"])
def test_unicode_text_produces_one_event_per_character(unix_input, text, mode):
    encoded = [character.encode("utf-8") for character in text]
    if mode == "typed":
        source = unix_input(encoded[0], encoded[1:])
    else:
        source = unix_input(b"".join(encoded))

    events = [snug._read_key_unix() for _ in text]

    assert events == ["space" if character == " " else character for character in text]
    assert not source.pending and not source.arrivals
    assert snug._read_key_timeout(0) is None


@pytest.mark.parametrize("character", ["é", "ệ", "📦"])
def test_multibyte_character_survives_separate_readiness_events(unix_input, character):
    encoded = character.encode("utf-8")
    source = unix_input(encoded[:1], [encoded[index:index + 1] for index in range(1, len(encoded))])

    assert snug._read_key_unix() == character
    assert source.waits and all(0 < timeout <= 0.05 for timeout in source.waits)
    assert not source.pending and not source.arrivals


@pytest.mark.parametrize("prefix", [b"[", b"O", b"[1;5"], ids=["CSI", "SS3", "modified-CSI"])
@pytest.mark.parametrize("final,event", [(b"A", "up"), (b"B", "down"), (b"C", "right"), (b"D", "left")])
def test_fragmented_arrows_inside_timeout_still_decode(unix_input, prefix, final, event):
    sequence = prefix + final
    source = unix_input(b"\x1b", [sequence[index:index + 1] for index in range(len(sequence))])

    assert snug._read_key_unix() == event
    assert source.waits and all(timeout == 0.05 for timeout in source.waits)
    assert not source.pending and not source.arrivals


@pytest.mark.parametrize(
    "payload,event",
    [
        (b"\r", "enter"),
        (b"\n", "enter"),
        (b" ", "space"),
        (b"\x7f", "backspace"),
        (b"\x08", "backspace"),
        (b"\x00", "other"),
        (b"\t", "other"),
        (b"\x1f", "other"),
        (b"\xc2\x80", "other"),
        (b"\xc2\x85", "other"),
        (b"\xc2\x9f", "other"),
        (b"\xc2\xad", "other"),
        (b"\xe2\x80\x8b", "other"),
    ],
)
def test_controls_and_nonprintable_unicode_keep_key_semantics(unix_input, payload, event):
    source = unix_input(payload)

    assert snug._read_key_unix() == event
    assert not source.pending


@pytest.mark.parametrize(
    "invalid",
    [
        b"\x80",                 # stray continuation
        b"\xc0\xaf",             # overlong two-byte encoding
        b"\xc1\xbf",             # invalid lead byte
        b"\xe0\x80\xaf",         # overlong three-byte encoding
        b"\xed\xa0\x80",         # surrogate
        b"\xf0\x80\x80\xaf",     # overlong four-byte encoding
        b"\xf4\x90\x80\x80",     # outside the Unicode range
        b"\xf5\x80\x80\x80",     # unsupported lead byte
        b"\xff",
    ],
)
def test_invalid_utf8_is_discarded_without_losing_following_text(unix_input, invalid):
    source = unix_input(invalid + "zViệt".encode("utf-8"))
    bad_events = []
    for _ in range(len(invalid) + 1):
        event = snug._read_key_unix()
        if event == "z":
            break
        bad_events.append(event)
    else:
        pytest.fail("invalid bytes consumed or obscured the following printable key")

    assert bad_events and set(bad_events) == {"other"}
    assert [snug._read_key_unix() for _ in "Việt"] == list("Việt")
    assert not source.pending


@pytest.mark.parametrize("prefix", [b"\xc2", b"\xe1\xbb", b"\xf0\x9f\x93"])
@pytest.mark.parametrize(
    "following,event",
    [
        (b"x", "x"),
        ("ệ".encode("utf-8"), "ệ"),
        (b"\r", "enter"),
        (b" ", "space"),
        (b"\x7f", "backspace"),
        (b"\x1b[A", "up"),
    ],
)
def test_noncontinuation_byte_after_bad_utf8_is_not_lost(unix_input, prefix, following, event):
    source = unix_input(prefix + following)

    assert snug._read_key_unix() == "other"
    assert snug._read_key_unix() == event
    assert not source.pending


@pytest.mark.parametrize("prefix", [b"\xc2", b"\xe1\xbb", b"\xf0\x9f\x93"])
def test_truncated_utf8_times_out_and_later_text_remains_readable(unix_input, prefix):
    source = unix_input(prefix, [None, "ệ".encode("utf-8")])

    assert snug._read_key_unix() == "other"
    assert source.waits and all(0 < timeout <= 0.05 for timeout in source.waits)
    assert snug._read_key_unix() == "ệ"
    assert not source.pending and not source.arrivals


@pytest.mark.parametrize("prefix", [b"\xc2", b"\xe1\xbb", b"\xf0\x9f\x93"])
def test_eof_during_utf8_is_safe_then_still_interrupts(unix_input, prefix):
    source = unix_input(prefix, [b""])

    assert snug._read_key_unix() == "other"
    with pytest.raises(KeyboardInterrupt):
        snug._read_key_unix()
    assert not source.pending and not source.arrivals


def test_bare_escape_preserves_50ms_wait(unix_input):
    source = unix_input(b"\x1b")

    assert snug._read_key_unix() == "esc"
    assert source.waits == [0.05]


@pytest.mark.parametrize("following", ["x", "ệ", "📦"])
def test_unrecognized_escape_prefix_preserves_next_character(unix_input, following):
    source = unix_input(b"\x1b" + following.encode("utf-8"))

    assert snug._read_key_unix() == "esc"
    assert snug._read_key_unix() == following
    assert not source.pending


@pytest.mark.parametrize("prefix,first_event", [(b"\x1b", "esc"), (b"\xc2", "other")])
def test_timeout_reader_delivers_pending_byte_without_descriptor_readiness(unix_input, prefix, first_event):
    source = unix_input(prefix + b"x")

    assert snug._read_key_unix() == first_event
    assert not source.pending
    waits_before = list(source.waits)
    assert snug._read_key_timeout(0.2) == "x"
    assert source.waits == waits_before
    assert snug._read_key_timeout(0) is None


def test_eof_at_start_still_interrupts(unix_input):
    unix_input(b"", eof=True)

    with pytest.raises(KeyboardInterrupt):
        snug._read_key_unix()


@pytest.mark.parametrize("prefix", [b"", b"\xc2", b"\xe1\xbb", b"\xf0\x9f\x93"])
def test_ctrl_c_interrupts_even_during_utf8(unix_input, prefix):
    source = unix_input(prefix + b"\x03")

    with pytest.raises(KeyboardInterrupt):
        snug._read_key_unix()
    assert not source.pending


@pytest.mark.parametrize("character", ["ệ", "📦", "\u0301"])
def test_picker_filter_uses_whole_unicode_events_and_backspace_preserves_paths(unix_input, tmp_path, character):
    source_path = tmp_path / f"Vi{character} exact path.txt"
    another = tmp_path / "Vi other.txt"
    for path in (source_path, another):
        path.touch()
    screen = snug._PickerScreen(tmp_path)
    screen.cursor = screen.entries.index(source_path)
    screen.handle("space")
    query = f"Vi{character}"
    source = unix_input(b"/" + query.encode("utf-8") + b"\x7f\r")

    assert screen.handle(snug._read_key_unix()) is None
    for _ in query:
        assert screen.handle(snug._read_key_unix()) is None
    assert screen.filter == query
    assert screen._visible() == [source_path]
    assert screen.marked == {source_path}

    assert screen.handle(snug._read_key_unix()) is None
    assert screen.filter == "Vi"
    assert set(screen._visible()) == {source_path, another}
    assert screen.marked == {source_path}
    assert screen.handle(snug._read_key_unix()) is snug._EXIT
    assert screen.result == [source_path]
    assert not source.pending


@pytest.mark.parametrize("character", ["é", "ｒ", "１", "𝟙", "ℚ", "📦"])
def test_non_ascii_key_cannot_activate_an_ascii_menu_shortcut(unix_input, character):
    unix_input(character.encode("utf-8"))
    screen = snug._MenuScreen("Actions", None, [("r", "Run"), ("1", "First"), ("q", "Quit")])
    screen.selected = 1

    assert screen.handle(snug._read_key_unix()) is None
    assert screen.selected == 1
    assert screen.result is None
    assert screen.handle("enter") is snug._EXIT
    assert screen.result == "1"
