"""Unreadable ZIPs report permissions even when optional backends are absent."""

import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "snug.py"


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0,
                    reason="requires a non-root POSIX account for file permissions")
@pytest.mark.parametrize("command", ["test", "extract", "list", "info"])
def test_unreadable_zip_without_optional_backends_reports_permission(tmp_path, command):
    archive = tmp_path / "unreadable.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("payload.txt", b"payload")
    destination = tmp_path / "output"
    args = [command, str(archive)]
    if command == "extract":
        args += ["-C", str(destination)]
    previous_mode = archive.stat().st_mode
    try:
        archive.chmod(0)
        result = subprocess.run(
            [sys.executable, "-B", "-S", str(SCRIPT), *args],
            env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1", NO_COLOR="1"),
            capture_output=True, text=True, timeout=45,
        )
    finally:
        archive.chmod(previous_mode)
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert result.stderr == (
        f"error: archive '{archive}': permission denied; check the file permissions\n"
    )
    assert not destination.exists()
