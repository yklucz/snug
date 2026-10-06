"""Run on Windows CI, including the no-existing-Python installation path."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires a Windows host")
ROOT = Path(__file__).resolve().parents[1]


def test_windows_bootstrap_password_workflow_and_deleted_dll_repair(tmp_path):
    app = tmp_path / "Snug runtime with spaces"
    app.mkdir()
    for name in ("snug.py", "snug_core.py", "snug_ext.py", "snug_runtime.py", "runtime.ps1", "runtime-lock.json"):
        shutil.copyfile(ROOT / name, app / name)
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = dict(os.environ)
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    for key in ("LIBARCHIVE", "SNUG_LIBRARY", "SNUG_PACKAGES"):
        env.pop(key, None)
    def run(*args):
        return subprocess.run([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(app / "runtime.ps1"), *map(str, args)], env=env,
                              capture_output=True, text=True, timeout=240)
    version = run("--version")
    assert version.returncode == 0, version.stderr
    assert "snug 1.8.0" in version.stdout
    assert (app / "python/python.exe").is_file()
    assert not list(app.rglob("pip"))
    assert not list(app.glob(".repair-*"))
    source = tmp_path / "source with spaces.txt"
    source.write_text("managed Windows payload")
    secret = tmp_path / "password.txt"
    secret.write_text("windows-test-secret\n")
    archive = tmp_path / "protected.7z"
    for args in (("create", archive, source, "--password-file", secret, "-q"),
                 ("extract", archive, "-C", tmp_path / "output with spaces", "--password-file", secret, "-q")):
        result = run(*args)
        assert result.returncode == 0, result.stderr
        assert "windows-test-secret" not in result.stdout + result.stderr
    assert (tmp_path / "output with spaces" / source.name).read_text() == source.read_text()
    fixture = ROOT / "tests/fixtures/test_read_format_rar.rar"
    assert run("list", fixture).returncode == 0
    dll = app / "native/libarchive-13.dll"
    dll.unlink()
    repaired = run("list", fixture)
    assert repaired.returncode == 0, repaired.stderr
    assert dll.is_file()
    assert not list(app.glob(".repair-*"))
    assert not list(app.glob(".python-*"))
