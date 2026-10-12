"""TUI results remain readable and reachable in both supported viewports."""

import io
import os
import re
from types import SimpleNamespace
import unicodedata

import pytest

import snug
from test_tui_editor import tty_prompt
from test_tui_input import unix_input
from test_tui_layout import _cells, _terminal_rows


SIZES = [(80, 24), (40, 10)]


class TtyOutput(io.StringIO):
    def isatty(self):
        return True


def rendered(screen, size, monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(snug.sys, "stdout", output)
    body = screen.draw(*size)
    footer = screen.footer()
    snug._draw_lines(body, footer, size=size)
    rows = _terminal_rows(output.getvalue(), size)
    assert len(body) + len(footer) <= size[1]
    assert all(_cells(line) <= size[0] - 1 for line in [*body, *footer])
    return body, footer, rows


def collect_result(monkeypatch, action):
    output = TtyOutput()
    monkeypatch.setattr(snug.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(snug.sys, "stdout", output)
    monkeypatch.setattr(snug.sys, "stderr", output)
    monkeypatch.setattr(snug, "_TUI_LAST_LINES", [])
    monkeypatch.setattr(snug, "_clear_screen", lambda: None)
    action()
    return list(snug._TUI_LAST_LINES) or output.getvalue().splitlines()


def skipped_report(tmp_path):
    report = snug.CreateReport(tmp_path / "created.zip", snug.ArchiveFormat.ZIP,
                               files=1, bytes_in=123, bytes_out=100, elapsed=1,
                               included_inputs=1, accidental_skips=25)
    report.skip_errors = [snug.CreateSkip(tmp_path / f"omitted-{number:02d}.txt", "permission denied")
                          for number in range(25)]
    return report


@pytest.mark.parametrize("size", SIZES)
def test_many_create_skips_keep_status_counts_and_normal_summary_first(tmp_path, monkeypatch, size):
    report = skipped_report(tmp_path)
    lines = collect_result(monkeypatch, lambda: snug._print_menu_create_result(report, None))
    meaningful = [line for line in lines if line.strip()]
    assert "incomplete" in meaningful[0].lower()
    assert "1 included" in meaningful[1] and "25 skipped" in meaningful[1]
    first_warning = next(index for index, line in enumerate(lines) if "warning: skipped" in line)
    summary = next(index for index, line in enumerate(lines) if "Created " in line)
    assert summary < first_warning
    body, footer, rows = rendered(snug._PauseScreen(lines), size, monkeypatch)
    visible = "\n".join(rows.values())
    assert "incomplete" in visible.lower() and "1 included" in visible and "25 skipped" in visible
    assert "hidden" in body[-1] and "lines" in body[-1]
    assert "Enter" in "\n".join(footer) and "Ctrl+C" in "\n".join(footer)


@pytest.mark.parametrize("size", SIZES)
def test_create_counts_include_intentional_skips_only_in_tui(tmp_path, monkeypatch, size):
    report = skipped_report(tmp_path)
    report.skipped = ["intentional-link"]
    cli_lines = collect_result(monkeypatch, lambda: snug._print_create_counts(report))
    assert "25 skipped" in "\n".join(cli_lines)
    lines = collect_result(monkeypatch, lambda: snug._print_menu_create_result(report, None))
    meaningful = [line for line in lines if line.strip()]
    assert "incomplete" in meaningful[0].lower()
    assert "1 included" in meaningful[1] and "26 skipped" in meaningful[1]
    _, _, rows = rendered(snug._PauseScreen(lines), size, monkeypatch)
    visible = "\n".join(rows.values())
    assert "incomplete" in visible.lower() and "1 included" in visible and "26 skipped" in visible


@pytest.mark.parametrize("size", SIZES)
def test_extract_result_keeps_status_counts_and_summary_before_skip_details(tmp_path, monkeypatch, size):
    destination = tmp_path / ("界e\u0301 folder " * 30 + "destination-end")
    report = snug.ExtractReport(tmp_path / "archive.zip", destination, files=2,
                                directories=3, symlinks=1, bytes_written=123, elapsed=1,
                                skipped=[f"omitted-{number:02d}.txt" for number in range(25)])
    lines = collect_result(monkeypatch, lambda: snug._print_menu_extract_result(report))
    meaningful = [line for line in lines if line.strip()]
    assert "Extraction complete." in meaningful[0]
    assert "6 included" in meaningful[1] and "25 skipped" in meaningful[1]
    summary = next(index for index, line in enumerate(lines) if "Extracted 6 items to " in line)
    detail = next(index for index, line in enumerate(lines) if "omitted-00.txt" in line)
    assert summary < detail
    body, _, rows = rendered(snug._PauseScreen(lines), size, monkeypatch)
    visible = "\n".join(rows.values())
    assert "Extraction complete." in visible and "6 included" in visible and "25 skipped" in visible
    assert "destination-end" in visible
    assert "hidden" in body[-1] and "lines" in body[-1]


@pytest.mark.parametrize("size", SIZES)
def test_create_error_puts_counts_and_cause_before_skip_details(tmp_path, monkeypatch, size):
    report = skipped_report(tmp_path)
    cause = "publication denied: choose a writable destination"
    error = snug.CreateError(cause, report, preserved_existing=True)
    lines = collect_result(monkeypatch, lambda: snug._print_menu_create_error(error, None))
    meaningful = [line for line in lines if line.strip()]
    assert "error:" in meaningful[0] and "preserved" in meaningful[0]
    first_warning = next(index for index, line in enumerate(lines) if "warning: skipped" in line)
    assert next(index for index, line in enumerate(lines) if "1 included" in line) < first_warning
    assert next(index for index, line in enumerate(lines) if cause in line) < first_warning
    _, _, rows = rendered(snug._PauseScreen(lines), size, monkeypatch)
    visible = "\n".join(rows.values())
    assert "preserved" in visible and "25 skipped" in visible and "publication denied" in visible


@pytest.mark.parametrize("size", SIZES)
def test_test_result_starts_with_success_and_counts_before_archive_path(tmp_path, monkeypatch, size):
    report = snug.TestReport(tmp_path / ("界é" * 60 + "-end.zip"), snug.ArchiveFormat.ZIP,
                             entries=3, files=2, bytes_read=123)
    lines = collect_result(monkeypatch, lambda: snug._print_menu_test_result(report))
    meaningful = [line for line in lines if line.strip()]
    assert "Archive is OK." in meaningful[0]
    assert "3 entries" in meaningful[1] and "2 files" in meaningful[1]
    _, _, rows = rendered(snug._PauseScreen(lines), size, monkeypatch)
    visible = "\n".join(rows.values())
    assert "Archive is OK." in visible and "3 entries" in visible and "-end.zip" in visible


@pytest.mark.parametrize("name", ["界é", "界e\u0301", "ＡÅ", "ＡA\u030a"])
@pytest.mark.parametrize("width", [20, 39, 79])
def test_middle_ellipsis_keeps_path_tail_and_combining_clusters(name, width):
    path = "/Volumes/" + (name + " directory/") * 12 + name + "-end.txt"
    shortened = snug._tui_middle(path, width)
    assert _cells(shortened) <= width
    assert shortened.startswith("/") and shortened.endswith("-end.txt")
    assert "…" in shortened
    suffix = shortened.split("…", 1)[1]
    assert not suffix or not unicodedata.combining(suffix[0])


@pytest.mark.parametrize("text,width", [("short.txt", 20), ("界e\u0301.txt", 20)])
def test_middle_ellipsis_preserves_short_path_exactly(text, width):
    assert snug._tui_middle(text, width) == text


@pytest.mark.parametrize("name", ["界é", "界e\u0301", "ＡÅ", "ＡA\u030a"])
@pytest.mark.parametrize("width", [10, 39, 79])
def test_long_causes_wrap_without_losing_unicode_or_words(name, width):
    cause = ("decoder rejected " + name * 8 + " because payload is incomplete ") * 3
    lines = snug._tui_wrap(cause, width)
    assert len(lines) > 1 and all(_cells(line) <= width for line in lines)
    assert re.sub(r"\s+", "", "".join(lines)) == re.sub(r"\s+", "", cause)
    assert all(not line or not unicodedata.combining(line[0]) for line in lines)
    assert "…" not in "".join(lines)


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("quote", ["", "'"])
def test_long_cause_url_is_wrapped_in_full_rather_than_shortened_as_a_path(monkeypatch, size, quote):
    message = "error: Read " + quote + "https://example.invalid/" + "details" * 30 + quote + " for the complete cause."
    screen = snug._PauseScreen([message])
    rendered(screen, size, monkeypatch)
    assert "".join("".join(screen._wrapped).split()) == "".join(message.split())


@pytest.mark.parametrize("size", SIZES)
def test_relative_unicode_member_path_uses_middle_ellipsis_and_keeps_filename(monkeypatch, size):
    member = "../" + "資料漢字e\u0301-😀-" * 30 + "THE-END.txt"
    screen = snug._PauseScreen([f"error: unsafe archive: path traversal in member: {member!r}"])
    body, _, _ = rendered(screen, size, monkeypatch)
    assert "…" in "\n".join(body) and "THE-END.txt" in "\n".join(body)
    assert len(screen._wrapped) <= 4


@pytest.mark.parametrize("size", SIZES)
def test_long_unicode_error_wraps_cause_and_retains_quoted_path_tail(monkeypatch, size):
    path = "/Volumes/资料 with spaces/" + "界e\u0301 folder/" * 15 + "archive-end.zip"
    cause = "decoder rejected the next member because its data was incomplete " * 40 + "CAUSE-END"
    screen = snug._PauseScreen([f"error: archive '{path}': {cause}"])
    body, _, rows = rendered(screen, size, monkeypatch)
    assert "archive-end.zip" in "\n".join(screen._wrapped)
    wrapped = re.sub(r"\s+", "", "".join(screen._wrapped))
    assert re.sub(r"\s+", "", cause) in wrapped
    assert "hidden" in body[-1] and "lines" in body[-1]
    assert "error:" in "\n".join(rows.values())
    screen.handle("end")
    _, _, final_rows = rendered(screen, size, monkeypatch)
    assert "CAUSE-END" in "\n".join(final_rows.values())


@pytest.mark.parametrize("size", SIZES)
def test_overflow_position_and_every_scroll_key_keep_all_rows_reachable(monkeypatch, size):
    source = [f"detail-{number:02d}" for number in range(50)]
    screen = snug._PauseScreen(source)
    body, footer, _ = rendered(screen, size, monkeypatch)
    assert len(footer) == (2 if size == (80, 24) else 1)
    assert screen._offset == 0 and screen._page > 0 and screen.lines == source
    page = screen._page
    maximum = len(screen._wrapped) - page
    assert body[-1].startswith(str(len(source) - page)) or str(len(source) - page) in body[-1]
    assert re.search(r"lines\s+1[-–]" + str(page) + r"\s+of\s+50", body[-1])
    for key, expected in [("up", 0), ("home", 0), ("down", 1), ("page_down", 1 + page),
                          ("page_up", 1), ("end", maximum), ("down", maximum),
                          ("page_down", maximum), ("up", maximum - 1), ("home", 0)]:
        assert screen.handle(key) is None
        assert screen._offset == min(maximum, expected)
        body, _, _ = rendered(screen, size, monkeypatch)
        assert "hidden" in body[-1] and "lines" in body[-1]
    encountered = set()
    for _ in range(len(source)):
        body, _, _ = rendered(screen, size, monkeypatch)
        encountered.update(line.strip() for line in body if line.startswith("detail-"))
        screen.handle("down")
    assert encountered == set(source)
    assert screen.lines == source


def test_pause_scroll_offset_stays_valid_after_resize(monkeypatch):
    screen = snug._PauseScreen([f"detail-{number:02d}" for number in range(50)])
    rendered(screen, (40, 10), monkeypatch)
    screen.handle("end")
    rendered(screen, (80, 24), monkeypatch)
    assert 0 <= screen._offset <= len(screen._wrapped) - screen._page
    body, _, _ = rendered(screen, (80, 24), monkeypatch)
    assert "detail-49" in "\n".join(body)
    rendered(screen, (39, 9), monkeypatch)
    body, _, _ = rendered(screen, (40, 10), monkeypatch)
    assert "hidden" in body[-1] and screen.lines[-1] == "detail-49"


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("dismiss", ["enter", "esc", KeyboardInterrupt()], ids=["Enter", "Escape", "Ctrl-C"])
def test_scrolled_pause_keeps_legacy_dismissal_contract(tty_prompt, monkeypatch, size, dismiss):
    screen = snug._PauseScreen([f"detail-{number:02d}" for number in range(50)])
    output, _ = tty_prompt(["end", dismiss])
    monkeypatch.setattr(snug, "_term_size", lambda: size)
    assert snug._run_screen(screen) is None
    assert screen._offset > 0
    assert "detail-49" in "\n".join(_terminal_rows(output.getvalue(), size).values())


@pytest.mark.skipif(os.name != "posix", reason="Unix terminal key reader")
@pytest.mark.parametrize("sequence,event", [(b"[5~", "page_up"), (b"[6~", "page_down"),
                                            (b"[5;2~", "page_up"), (b"[6;5~", "page_down")])
@pytest.mark.parametrize("fragmented", [False, True])
def test_existing_unix_reader_decodes_page_keys_and_preserves_following_text(unix_input, sequence, event, fragmented):
    source = (unix_input(b"\x1b", [sequence[index:index + 1] for index in range(len(sequence))] + [b"x"])
              if fragmented else unix_input(b"\x1b" + sequence + b"x"))
    assert snug._read_key_unix() == event
    assert snug._read_key_unix() == "x"
    assert not source.pending and not source.arrivals


@pytest.mark.parametrize("encoded,event", [(b"I", "page_up"), (b"Q", "page_down"),
                                           (b"G", "home"), (b"O", "end")])
def test_existing_windows_reader_maps_scroll_keys_without_a_new_input_path(encoded, event):
    assert snug._decode_extended_windows(lambda: encoded) == event

