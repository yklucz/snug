"""Updater commands and notice filtering preserve explicit archive CLI output."""
import json

import pytest

import snug
import snug_update as update


@pytest.fixture
def isolated_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "state_path", lambda: tmp_path / "state/update.json")
    monkeypatch.setenv("SNUG_NO_UPDATE_CHECK", "1")
    return tmp_path


@pytest.mark.parametrize("latest,available", [("1.8.0", False), ("1.10.0", True)])
def test_explicit_update_check_output(isolated_updates, monkeypatch, capsys, latest, available):
    monkeypatch.setattr(update, "check_update", lambda version: update.UpdateResult(version, latest, available))
    monkeypatch.setattr(snug, "ArchiveEngine", lambda: pytest.fail("update must not open an archive engine"))
    assert snug.main(["update", "--check"]) == 0
    output = capsys.readouterr()
    assert not output.err
    if available:
        assert f"Current version: {snug.__version__}" in output.out
        assert f"Latest version:  {latest}" in output.out
        assert "Run `snug update` to install it." in output.out
    else:
        assert output.out == f"Snug {snug.__version__} is up to date.\n"


def test_update_failure_has_normal_cli_error(isolated_updates, monkeypatch, capsys):
    def unavailable(version):
        raise update.UpdateError("network unavailable")
    monkeypatch.setattr(update, "check_update", unavailable)
    assert snug.main(["update", "--check"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert output.err == "error: network unavailable\n"


def test_update_preferences_do_not_contact_network(isolated_updates, monkeypatch, capsys):
    monkeypatch.setattr(update, "_request", lambda *args: pytest.fail("preferences must be local"))
    for option, enabled in [("--disable-checks", False), ("--enable-checks", True)]:
        assert snug.main(["update", option]) == 0
        assert json.loads(update.state_path().read_text())["automatic_checks"] is enabled
    assert "enabled" in capsys.readouterr().out


def test_manual_update_dispatch(isolated_updates, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(update, "perform_update", lambda version: calls.append(version) or "Updated Snug.")
    assert snug.main(["update"]) == 0
    assert calls == [snug.__version__]
    assert capsys.readouterr().out == "Updated Snug.\n"


def test_mutually_exclusive_update_actions():
    with pytest.raises(SystemExit) as exc:
        snug._build_parser().parse_args(["update", "--check", "--disable-checks"])
    assert exc.value.code == 2


@pytest.mark.parametrize("command,quiet,notice", [
    ("create", False, True), ("create", True, False),
    ("extract", False, True), ("extract", True, False),
    ("list", False, False), ("info", False, False),
])
def test_automatic_notice_never_pollutes_listing_or_quiet_output(
    isolated_updates, tmp_path, monkeypatch, capsys, command, quiet, notice,
):
    source = tmp_path / "hello.txt"
    source.write_bytes(b"notice filter")
    archive = tmp_path / "hello.zip"
    snug.ArchiveEngine().create(archive, [source])
    handle = update.AutomaticCheck(update.UpdateResult(snug.__version__, "1.10.0", True))
    handle.done.set()
    monkeypatch.setattr(snug, "_start_update_check", lambda: handle)
    monkeypatch.setattr(update, "automatic_notice", lambda value: "NOTICE: run snug update")
    monkeypatch.setattr(snug.sys.stderr, "isatty", lambda: True)
    if command == "create":
        args = [command, str(tmp_path / "new.zip"), str(source)]
    elif command == "extract":
        args = [command, str(archive), "-C", str(tmp_path / "out")]
    else:
        args = [command, str(archive)]
    if quiet:
        args.append("--quiet")
    assert snug.main(args) == 0
    output = capsys.readouterr()
    assert "NOTICE" not in output.out
    assert ("NOTICE" in output.err) is notice


def test_optional_check_failure_cannot_fail_archive_command(isolated_updates, tmp_path, monkeypatch):
    def broken(version):
        raise OverflowError("corrupt optional state")
    monkeypatch.setattr(update, "start_automatic_check", broken)
    source = tmp_path / "source.txt"
    source.write_bytes(b"offline")
    assert snug.main(["create", str(tmp_path / "result.zip"), str(source), "--quiet"]) == 0


def test_optional_notice_failure_cannot_fail_archive_command(isolated_updates, tmp_path, monkeypatch):
    def broken(handle):
        raise OSError("optional state became unavailable")
    monkeypatch.setattr(update, "automatic_notice", broken)
    monkeypatch.setattr(snug.sys.stderr, "isatty", lambda: True)
    source = tmp_path / "source.txt"
    source.write_bytes(b"offline")
    assert snug.main(["create", str(tmp_path / "result.zip"), str(source)]) == 0


def test_optional_notice_output_failure_is_harmless(monkeypatch):
    class FailedTerminal:
        def isatty(self):
            return True

        def write(self, text):
            raise OSError("terminal output closed")

    monkeypatch.setattr(update, "automatic_notice", lambda handle: "Update available")
    monkeypatch.setattr(snug.sys, "stderr", FailedTerminal())
    snug._update_notice(None)
