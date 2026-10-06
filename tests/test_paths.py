"""Offline CLI regressions for paths passed by a terminal or subprocess caller."""

import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

import snug


SCRIPT = Path(__file__).resolve().parents[1] / "snug.py"
PATH_NAMES = [
    "My Folder",
    "Unicode café Ω",
    "Tài liệu tiếng Việt",
    "archive 📦",
    'double "quotes"',
    "author's notes",
    "-important-file",
    "long-" + "n" * 195,
    "many.dots.in.a.name",
    "notes (draft) [final]",
]


def run_cli(tmp_path, *args):
    env = os.environ.copy()
    env.update({
        "SNUG_NO_UPDATE_CHECK": "1",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    })
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )


def assert_success(result):
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("name", PATH_NAMES)
@pytest.mark.parametrize("suffix", ["zip", "tar.gz"])
def test_explicit_commands_preserve_terminal_paths(tmp_path, name, suffix):
    if os.name == "nt" and '"' in name:
        pytest.skip("Windows forbids double quotes in physical filenames")
    source = tmp_path / name / "payload.txt"
    source.parent.mkdir()
    source.write_bytes(b"Terminal path round trip: \xe2\x9c\x93\n")
    archive = tmp_path / f"{name}.{suffix}"
    destination = tmp_path / f"restored {name}"

    # Each path is one argument, including literal quotes, brackets, and spaces.
    assert_success(run_cli(tmp_path, "create", archive, source.parent, "-q"))
    listed = run_cli(tmp_path, "list", archive)
    assert_success(listed)
    assert f"{name}/payload.txt" in listed.stdout
    inspected = run_cli(tmp_path, "info", archive)
    assert_success(inspected)
    assert "backend" in inspected.stdout
    assert_success(run_cli(tmp_path, "extract", archive, "-C", destination, "-q"))
    assert (destination / name / "payload.txt").read_bytes() == source.read_bytes()


def test_leading_hyphens_use_standard_argument_separator(tmp_path):
    source = tmp_path / "-important-file.txt"
    source.write_bytes(b"leading hyphen")
    archive_name = "-archive.zip"

    assert_success(run_cli(tmp_path, "create", "-q", "--", archive_name, source.name))
    listed = run_cli(tmp_path, "list", "--", archive_name)
    assert_success(listed)
    assert source.name in listed.stdout
    assert_success(run_cli(tmp_path, "info", "--", archive_name))
    assert_success(run_cli(tmp_path, "extract", "--directory=-output", "-q", "--", archive_name))
    assert (tmp_path / "-output" / source.name).read_bytes() == source.read_bytes()


def test_create_separator_accepts_hyphen_source_after_archive(tmp_path):
    source = tmp_path / "-important-file.txt"
    source.write_bytes(b"source after separator")
    assert_success(run_cli(tmp_path, "create", "-q", "archive.zip", "--", source.name))
    with zipfile.ZipFile(tmp_path / "archive.zip") as archive:
        assert archive.read(source.name) == source.read_bytes()


def test_create_preserves_existing_multiple_source_selection(tmp_path):
    first = tmp_path / "first file.txt"
    second = tmp_path / "ghi chú 📦.txt"
    folder = tmp_path / "Folder (selected) [two]"
    folder.mkdir()
    nested = folder / "author's notes.txt"
    for index, source in enumerate((first, second, nested)):
        source.write_bytes(f"source {index}".encode())

    assert_success(run_cli(tmp_path, "create", "backup.zip", first, second, folder, "-q"))
    assert_success(run_cli(tmp_path, "extract", "backup.zip", "-C", "restored", "-q"))
    for source in (first, second, nested):
        assert (tmp_path / "restored" / source.relative_to(tmp_path)).read_bytes() == source.read_bytes()


def test_nested_long_paths_preserve_contents(tmp_path):
    source_root = tmp_path / ("nested " + "a" * 100)
    source = source_root / ("b" * 100) / ("c" * 100 + ".txt")
    source.parent.mkdir(parents=True)
    source.write_bytes(b"nested long path")
    assert_success(run_cli(tmp_path, "create", "backup.zip", source_root, "-q"))
    assert_success(run_cli(tmp_path, "extract", "backup.zip", "-C", "restored", "-q"))
    assert (tmp_path / "restored" / source.relative_to(tmp_path)).read_bytes() == source.read_bytes()


@pytest.mark.parametrize("command", [
    "folder/", "archive.zip", "file.txt", "a", "x", "t", "archive", "open", "extract-here",
])
def test_implicit_paths_and_replacement_aliases_remain_invalid(tmp_path, command):
    (tmp_path / "folder").mkdir()
    (tmp_path / "file.txt").write_bytes(b"plain file")
    with zipfile.ZipFile(tmp_path / "archive.zip", "w") as archive:
        archive.writestr("file.txt", b"archive file")
    result = run_cli(tmp_path, command)
    assert result.returncode == 2
    assert "invalid choice" in result.stderr
    assert "Traceback" not in result.stderr


def test_no_arguments_keep_interactive_terminal_dispatch(monkeypatch):
    calls = []

    def interactive():
        calls.append("terminal")
        return 0

    monkeypatch.setattr(snug, "_launch_interactive", interactive)
    assert snug.main([]) == 0
    assert calls == ["terminal"]
