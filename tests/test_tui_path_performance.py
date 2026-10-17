"""Keep untrusted result paths responsive without changing their display rules."""

from pathlib import Path
import subprocess
import sys
import unicodedata

import pytest

import snug


@pytest.mark.parametrize("case", ["unclosed", "zip_member"])
def test_adversarial_quoted_result_finishes_in_a_bounded_child(case):
    # A separate process can be killed even while the regex engine holds the GIL.
    script = r'''
from pathlib import Path
import sys
import tempfile
import zipfile
import snug

if sys.argv[1] == "unclosed":
    line = "error: '" + "/" * 1_000_000
    assert snug._tui_pause_paths(line, 79) == line
else:
    with tempfile.TemporaryDirectory() as folder:
        archive = Path(folder) / "unsafe.zip"
        name = "'" + "/" * 65_531 + "../"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(name, b"")
        try:
            snug.ArchiveEngine(backends=[snug.NativeBackend()]).extract(
                archive, Path(folder) / "out")
        except snug.UnsafeArchiveError as exc:
            line = "error: unsafe archive: " + str(exc)
        else:
            raise AssertionError("fixture must reach the unsafe-member diagnostic")
        body = snug._PauseScreen([line]).draw(80, 24)
        assert body[0].startswith("error: unsafe archive:")
print("finished")
'''
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, case],
            cwd=Path(snug.__file__).resolve().parent,
            capture_output=True, text=True, timeout=5, check=True,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{case} result rendering exceeded the generous 5-second budget")
    assert result.stdout.strip() == "finished"


@pytest.mark.parametrize("line,expected", [
    ("error: '/alpha/beta/file.txt'", "error: '/alp…/file.txt'"),
    ('error: "/alpha/beta/file.txt"', 'error: "/alp…/file.txt"'),
    ("error: 'plain'/alpha/beta/file.txt'", "error: 'plain'/alp…/file.txt'"),
    ("error: 'short.txt'", "error: 'short.txt'"),
    ("error: 'https://example.invalid/alpha/beta/file.txt'",
     "error: 'https://example.invalid/alpha/beta/file.txt'"),
    ('error: "A+1.-://example.invalid/alpha/beta/file.txt"',
     'error: "A+1.-://example.invalid/alpha/beta/file.txt"'),
    ("error: 'https://host/path'and/filename'", "error: 'https://host/path'and/filename'"),
    ("error: 'unterminated/alpha/beta/file.txt", "error: 'unterminated/alpha/beta/file.txt"),
    ('error: \'mismatched/alpha/beta/file.txt"', 'error: \'mismatched/alpha/beta/file.txt"'),
    ("error: /alpha/beta/file.txt", "error: /alp…/file.txt"),
    ("error: 'C:\\alpha\\beta\\file.txt'", "error: 'C:\\a…\\file.txt'"),
])
def test_quoted_path_recognition_preserves_existing_display(line, expected):
    assert snug._tui_pause_paths(line, 24) == expected


@pytest.mark.parametrize("form", ["NFC", "NFD"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_unicode_quoted_paths_keep_normalization_clusters_and_filename(form, quote):
    path = unicodedata.normalize(form, "/資料/" + "界é-folder/" * 20 + "尾é-end.txt")
    result = snug._tui_pause_paths(f"error: {quote}{path}{quote}", 39)
    assert result.startswith(f"error: {quote}/資料/")
    assert result.endswith(unicodedata.normalize(form, f"尾é-end.txt{quote}"))
    assert "…" in result
    assert unicodedata.is_normalized(form, result)
    suffix = result.split("…", 1)[1]
    assert not unicodedata.combining(suffix[0])
    assert snug._tui_cell_width(result.removeprefix("error: ")) <= 31


@pytest.mark.parametrize("form", ["NFC", "NFD"])
def test_unicode_cause_and_quoted_url_are_retained_exactly(form):
    line = unicodedata.normalize(form, "error: 界é 'https://example.invalid/資料/界é' is unavailable")
    assert snug._tui_pause_paths(line, 24) == line
