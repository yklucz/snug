"""Check an installed entry point and updater offline, outside the checkout."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
import importlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
from unittest.mock import patch
import urllib.error


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def check_installation() -> None:
    distribution = importlib.metadata.distribution("snug-archives")
    version = distribution.version
    executable = Path(sysconfig.get_path("scripts")) / ("snug.exe" if os.name == "nt" else "snug")
    launched = subprocess.run([str(executable), "--version"], capture_output=True, text=True, check=True)
    require(launched.stdout == f"snug {version}\n" and not launched.stderr,
            "Installed CLI version differs from its distribution metadata")

    modules = {name: importlib.import_module(name)
               for name in ("snug", "snug_core", "snug_ext", "snug_update")}
    entries = [entry for entry in distribution.entry_points
               if entry.group == "console_scripts" and entry.name == "snug"]
    require(len(entries) == 1 and entries[0].value == "snug:main", "Unexpected snug entry point")
    main = entries[0].load()
    updater = modules["snug_update"]

    def invoke(arguments: list[str], code: int, out: str = "", err: str = "") -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", [str(executable), *arguments]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            require(main() == code, f"Unexpected exit code for {arguments}")
        require(stdout.getvalue() == out and stderr.getvalue() == err,
                f"Unexpected output for {arguments}: {stdout.getvalue()!r} {stderr.getvalue()!r}")

    with tempfile.TemporaryDirectory(prefix="snug-installed-check-") as directory, \
            patch.object(updater, "state_path", lambda: Path(directory) / "update.json"), \
            patch.object(modules["snug"], "ArchiveEngine", side_effect=AssertionError("Updater opened an archive engine")), \
            patch.object(updater, "perform_update", side_effect=AssertionError("Check attempted an installation")):
        release = json.dumps({"tag_name": "v" + version, "draft": False, "prerelease": False}).encode()
        with patch.object(updater, "_request", return_value=io.BytesIO(release)) as request:
            invoke(["update", "--check"], 0, out=f"Snug {version} is up to date.\n")
            request.assert_called_once_with(updater.API_URL, updater.REQUEST_TIMEOUT)

        for error, message in (
            (urllib.error.HTTPError(updater.API_URL, 404, "missing", Message(), None),
             "no published stable GitHub release is available"),
            (urllib.error.URLError("offline fixture"), "network unavailable"),
        ):
            with patch.object(updater, "_request", side_effect=error) as request:
                invoke(["update", "--check"], 2, err=f"error: {message}\n")
                request.assert_called_once_with(updater.API_URL, updater.REQUEST_TIMEOUT)

        with patch.object(updater, "_request", side_effect=AssertionError("Preferences contacted the network")):
            for option, enabled in (("--disable-checks", False), ("--enable-checks", True)):
                invoke(["update", option], 0,
                       out=f"Automatic update checks {'enabled' if enabled else 'disabled'}.\n")
                require(json.loads(updater.state_path().read_text())["automatic_checks"] is enabled,
                        "Update preference was not saved")

    print(f"Installed CLI: Snug {version}, four module imports, offline updater checks and preferences verified")


def main() -> int:
    try:
        check_installation()
    except (AssertionError, ImportError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Installed CLI check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
