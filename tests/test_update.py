"""Update behavior is exercised offline in temporary, synthetic installations."""
from __future__ import annotations

from email.message import Message
import gzip
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import threading
from types import SimpleNamespace
import urllib.error
import urllib.response

import pytest

import snug_update as update

CURRENT = "1.8.0"
LATEST = "1.10.2"
REAL_STATE_PATH = update.state_path
REAL_REQUEST = update._request


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    path = tmp_path / "state/update.json"
    monkeypatch.setattr(update, "state_path", lambda: path)
    monkeypatch.delenv("SNUG_NO_UPDATE_CHECK", raising=False)
    # Any unexpected request is a test failure rather than an internet access.
    monkeypatch.setattr(update, "_request", lambda *args: pytest.fail("unexpected network request"))
    return path


def release(version=LATEST, **extra):
    return {"tag_name": "v" + version, "draft": False, "prerelease": False, **extra}


def metadata(monkeypatch, body):
    calls = []

    def request(url, timeout):
        calls.append((url, timeout))
        return io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())

    monkeypatch.setattr(update, "_request", request)
    return calls


def write_state(path, **values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values), encoding="utf-8")


@pytest.mark.parametrize(("old", "new", "available"), [
    (CURRENT, CURRENT, False), ("1.9.0", "1.10.0", True), ("1.10.0", "1.9.0", False),
    ("1.9.99", "2.0.0", True), ("2.0.0", "1.99.99", False),
    ("1.10.2-rc.1", LATEST, True), ("1.10.2+one", "1.10.2+two", False),
])
def test_manual_check_semantic_precedence(monkeypatch, isolated_state, old, new, available):
    calls = metadata(monkeypatch, release(new))
    result = update.check_update(old)
    assert (result.latest_version, result.available) == (new, available)
    assert calls == [(update.API_URL, update.REQUEST_TIMEOUT)]
    saved = json.loads(isolated_state.read_text())
    assert saved["latest_version"] == new and saved["last_check"] > 0


@pytest.mark.parametrize(("left", "right"), [
    ("1.0.0-alpha", "1.0.0-alpha.1"), ("1.0.0-alpha.1", "1.0.0-alpha.beta"),
    ("1.0.0-alpha.beta", "1.0.0-beta"), ("1.0.0-beta", "1.0.0-beta.2"),
    ("1.0.0-beta.2", "1.0.0-beta.11"), ("1.0.0-beta.11", "1.0.0-rc.1"),
    ("1.0.0-rc.1", "1.0.0"),
])
def test_semver_prerelease_order(left, right):
    assert update.Version.parse(left) < update.Version.parse(right)


@pytest.mark.parametrize("invalid", ["1.2", "v1.2.3", "01.2.3", "1.02.3", "1.2.03",
                                      "1.2.3-alpha.01", "1.2.3-", "1.2.3+", "1.2.3\n", "", "x" * 129])
def test_invalid_semver_is_rejected(invalid):
    with pytest.raises(update.UpdateError, match="semantic version"):
        update.Version.parse(invalid)


@pytest.mark.parametrize("body", [release(prerelease=True), release(draft=True), release("2.0.0-rc.1")])
def test_stable_check_never_promotes_prerelease_or_draft(monkeypatch, body):
    metadata(monkeypatch, body)
    result = update.check_update(CURRENT)
    assert not result.available and result.release is None


@pytest.mark.parametrize("body", [b"not JSON", b"\xff", b"[]", b"null", {}, {"tag_name": "v2.0.0"},
                                      release(tag_name=123), release(draft="false"), release(prerelease=0)])
def test_invalid_json_and_release_schema(monkeypatch, body):
    metadata(monkeypatch, body)
    with pytest.raises(update.UpdateError, match="JSON|metadata"):
        update.check_update(CURRENT)


def test_invalid_release_version(monkeypatch):
    metadata(monkeypatch, release("newest"))
    with pytest.raises(update.UpdateError, match="semantic version"):
        update.check_update(CURRENT)


def test_release_response_is_bounded(monkeypatch):
    monkeypatch.setattr(update, "METADATA_LIMIT", 20)
    metadata(monkeypatch, b"x" * 21)
    with pytest.raises(update.UpdateError, match="size limit"):
        update.check_update(CURRENT)


def test_continuously_streaming_metadata_has_total_deadline(monkeypatch):
    metadata(monkeypatch, release())
    clock = iter([0.0, 0.0, update.REQUEST_TIMEOUT + 1.0])
    monkeypatch.setattr(update.time, "monotonic", lambda: next(clock))
    with pytest.raises(update.UpdateError, match="timed out"):
        update.check_update(CURRENT)


@pytest.mark.parametrize(("error", "message"), [
    (OSError("local username must not appear"), "network unavailable"),
    (urllib.error.URLError("DNS failure with local path"), "network unavailable"),
    (TimeoutError("socket timeout"), "timed out"),
    (urllib.error.URLError(TimeoutError("socket timeout")), "timed out"),
    (http.client.IncompleteRead(b"partial"), "network unavailable"),
    (urllib.error.HTTPError(update.API_URL, 403, "forbidden", None, None), "rate limit"),
    (urllib.error.HTTPError(update.API_URL, 429, "limited", None, None), "rate limit"),
    (urllib.error.HTTPError(update.API_URL, 404, "missing", None, None), "no published stable"),
    (urllib.error.HTTPError(update.API_URL, 500, "internal", None, None), "HTTP 500"),
])
def test_network_failures_have_safe_cli_errors(monkeypatch, error, message):
    def fail(*args):
        raise error

    monkeypatch.setattr(update, "_request", fail)
    with pytest.raises(update.UpdateError, match=message) as caught:
        update.check_update(CURRENT)
    assert "local" not in str(caught.value)


def test_manual_check_works_when_automatic_checks_are_disabled(monkeypatch, isolated_state):
    update.set_checks(False)
    monkeypatch.setenv("SNUG_NO_UPDATE_CHECK", "1")
    metadata(monkeypatch, release())
    assert update.check_update(CURRENT).available
    assert json.loads(isolated_state.read_text())["automatic_checks"] is False


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_platform_state_locations(monkeypatch, tmp_path, platform):
    home = tmp_path / 'home'
    xdg = tmp_path / 'xdg'
    local = tmp_path / 'local'
    monkeypatch.setattr(update.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    expected = {"darwin": home / 'Library/Application Support/Snug/update.json',
                "linux": xdg / 'snug/update.json', "win32": local / 'Snug/state/update.json'}
    assert REAL_STATE_PATH() == expected[platform]


def test_state_missing_corrupt_and_preferences(isolated_state):
    assert update._read_state() == {}
    write_state(isolated_state, automatic_checks=False, latest_version="bad version", last_check=True)
    assert update._read_state() == {"automatic_checks": False}
    isolated_state.write_text("{broken")
    assert update._read_state() == {}
    update.set_checks(False)
    assert update._read_state()["automatic_checks"] is False
    update.set_checks(True)
    assert update._read_state()["automatic_checks"] is True
    assert not list(isolated_state.parent.glob(".update-*"))


@pytest.mark.parametrize("stamp", [10 ** 1000, 1e12, float("nan"), float("inf")])
def test_corrupt_or_future_timestamp_cannot_disable_checks(monkeypatch, isolated_state, stamp):
    inline_workers(monkeypatch)
    monkeypatch.setattr(update.time, "time", lambda: 200000.0)
    write_state(isolated_state, last_check=stamp)
    metadata(monkeypatch, release())
    assert update.start_automatic_check(CURRENT)


def test_deeply_nested_state_is_treated_as_corrupt(isolated_state):
    isolated_state.parent.mkdir(parents=True)
    isolated_state.write_text("[" * 1500 + "0" + "]" * 1500)
    assert update._read_state() == {}


def test_lock_owner_process_exit_cannot_leave_stale_state_lock(monkeypatch, isolated_state):
    script = ("import os, sys; from pathlib import Path; import snug_update as u; "
              "u.state_path=lambda:Path(sys.argv[1]); lock=u._state_lock(); "
              "assert lock is not None; os._exit(0)")
    subprocess.run([update.sys.executable, "-c", script, str(isolated_state)], check=True,
                   cwd=Path(update.__file__).resolve().parent, timeout=10)
    assert isolated_state.with_suffix(".lock").is_file()
    inline_workers(monkeypatch)
    metadata(monkeypatch, release())
    assert update.start_automatic_check(CURRENT)


def test_state_write_failure_is_tolerated_but_preference_reports_it(monkeypatch, isolated_state):
    monkeypatch.setattr(update, "_write_state", lambda state: False)
    metadata(monkeypatch, release())
    assert update.check_update(CURRENT).available
    assert update.start_automatic_check(CURRENT) is None
    with pytest.raises(update.UpdateError, match="unwritable"):
        update.set_checks(False)
    assert not isolated_state.exists()


def test_atomic_state_write_failure_cleans_temporary_file(monkeypatch, isolated_state):
    monkeypatch.setattr(update.os, "replace", lambda *args: (_ for _ in ()).throw(PermissionError()))
    assert not update._write_state({"last_check": 1})
    assert not isolated_state.exists()
    assert not list(isolated_state.parent.glob(".update-*"))


def inline_workers(monkeypatch):
    class InlineThread:
        def __init__(self, *, target, name, daemon):
            assert name == "snug-update-check" and daemon is True
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(update.threading, "Thread", InlineThread)


def test_automatic_check_records_attempt_before_request_and_throttles(monkeypatch, isolated_state):
    inline_workers(monkeypatch)
    monkeypatch.setattr(update.time, "time", lambda: 200000.0)
    calls = []

    def request(url, timeout):
        calls.append(url)
        assert update._read_state()["last_check"] == 200000.0
        return io.BytesIO(json.dumps(release()).encode())

    monkeypatch.setattr(update, "_request", request)
    handle = update.start_automatic_check(CURRENT)
    assert handle and handle.done.is_set()
    assert update.automatic_notice(handle) == "Update available: Snug 1.10.2\nRun `snug update` to install."
    assert update.automatic_notice(handle) is None
    assert update.start_automatic_check(CURRENT) is None
    monkeypatch.setattr(update.time, "time", lambda: 200000.0 + update.CHECK_INTERVAL - 1)
    assert update.start_automatic_check(CURRENT) is None
    monkeypatch.setattr(update.time, "time", lambda: 200000.0 + update.CHECK_INTERVAL)
    assert update.start_automatic_check(CURRENT)
    assert calls == [update.API_URL, update.API_URL]


def test_failed_automatic_attempt_still_throttles(monkeypatch, isolated_state):
    inline_workers(monkeypatch)
    monkeypatch.setattr(update, "_request", lambda *args: (_ for _ in ()).throw(OSError("offline")))
    handle = update.start_automatic_check(CURRENT)
    assert handle and handle.done.is_set()
    assert update.automatic_notice(handle) is None
    assert update.start_automatic_check(CURRENT) is None


def test_automatic_request_is_nonblocking(monkeypatch):
    entered = threading.Event()
    finish = threading.Event()

    def request(*args):
        entered.set()
        assert finish.wait(2)
        return io.BytesIO(json.dumps(release()).encode())

    monkeypatch.setattr(update, "_request", request)
    handle = update.start_automatic_check(CURRENT)
    try:
        assert handle and entered.wait(1)
        assert not handle.done.is_set()
        assert update.automatic_notice(handle) is None
    finally:
        finish.set()
    assert handle and handle.done.wait(2)


def test_disabled_and_busy_state_prevent_automatic_requests(monkeypatch, isolated_state):
    monkeypatch.setenv("SNUG_NO_UPDATE_CHECK", "1")
    assert update.start_automatic_check(CURRENT) is None
    monkeypatch.delenv("SNUG_NO_UPDATE_CHECK")
    update.set_checks(False)
    assert update.start_automatic_check(CURRENT) is None
    update.set_checks(True)
    lock = update._state_lock()
    assert lock
    try:
        assert update.start_automatic_check(CURRENT) is None
    finally:
        update._unlock_state(lock)


def test_corrupt_state_allows_fresh_automatic_check(monkeypatch, isolated_state):
    inline_workers(monkeypatch)
    write_state(isolated_state, latest_version="bad")
    metadata(monkeypatch, release())
    assert update.start_automatic_check(CURRENT)


def test_notice_respects_newly_disabled_preference(monkeypatch):
    inline_workers(monkeypatch)
    metadata(monkeypatch, release())
    handle = update.start_automatic_check(CURRENT)
    update.set_checks(False)
    assert update.automatic_notice(handle) is None


@pytest.mark.parametrize("redirect", ["http://github.com/unsafe", "https://attacker.example/file",
                                      "https://github.com:444/file", "https://user:secret@github.com/file"])
def test_redirect_rejected_before_following(monkeypatch, redirect):
    requested = []

    class OfflineHTTPSHandler(update.urllib.request.HTTPSHandler):
        def https_open(self, request):
            requested.append(request.full_url)
            headers = Message()
            headers["Location"] = redirect
            response = urllib.response.addinfourl(io.BytesIO(), headers, request.full_url, 302)
            response.msg = "Found"
            return response

    opener = update.urllib.request.build_opener(update.HTTPSRedirectHandler(), OfflineHTTPSHandler())
    with pytest.raises(update.UpdateError, match="official GitHub HTTPS"):
        opener.open(update.API_URL)
    assert requested == [update.API_URL]


def test_official_https_asset_redirect_is_allowed():
    redirect = "https://release-assets.githubusercontent.com/github-production-release-asset/file?token=release-token"
    request = update.urllib.request.Request("https://github.com/yklucz/snug/releases/download/v1.10.2/file")
    redirected = update.HTTPSRedirectHandler().redirect_request(request, None, 302, "Found", Message(), redirect)
    assert redirected and redirected.full_url == redirect


def test_request_headers_send_no_installation_or_usage_data(monkeypatch):
    captured = []
    monkeypatch.setattr(update.urllib.request, "build_opener", lambda *handlers: SimpleNamespace(
        open=lambda request, timeout: captured.append((request, timeout)) or io.BytesIO(b"{}")))
    REAL_REQUEST(update.API_URL, 5)
    request, timeout = captured[0]
    assert request.full_url == update.API_URL and timeout == 5
    assert request.data is None
    assert request.header_items() == [("Accept", "application/vnd.github+json"), ("User-agent", "Snug-update"),
                                      ("X-github-api-version", "2022-11-28")]


def make_sdist(path, extra=(), missing=(), version=LATEST):
    root = "snug_archives-" + version
    with tarfile.open(path, "w:gz") as archive:
        folder = tarfile.TarInfo(root)
        folder.type = tarfile.DIRTYPE
        archive.addfile(folder)
        for name in update.OWNED_FILES:
            if name in missing:
                continue
            content = (f'print("snug {version}")\n' if name == "snug.py" else
                       "raise SystemExit(0)\n" if name == "snug_runtime.py" else
                       f"new {name}\n").encode()
            member = tarfile.TarInfo(f"{root}/{name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        for member, content in extra:
            archive.addfile(member, io.BytesIO(content) if content is not None else None)
    return path.read_bytes()


@pytest.fixture
def managed_install(tmp_path, monkeypatch):
    root = tmp_path / "Snug's space Tiếng Việt 📦 (app)"
    root.mkdir()
    for name in update.OWNED_FILES:
        (root / name).write_text(f"old {name}\n")
    (root / update.INSTALL_MARKER).write_text(json.dumps({"schema": 1, "kind": "homebrew", "branch": "main"}))
    for name in ("vendor", "packages", "native"):
        (root / name).mkdir()
        (root / name / "keep.bin").write_bytes(b"current runtime")
    (root / "runtime.json").write_text('{"kind":"homebrew"}')
    launcher = tmp_path / "snug-launcher"
    launcher.write_text(f"stable launcher referencing {root}", encoding="utf-8")
    monkeypatch.setattr(update, "APP", root)
    monkeypatch.setenv("SNUG_MANAGED_ROOT", str(root))
    monkeypatch.setattr(update.sys, "platform", "darwin")
    return root


def provide_update(monkeypatch, archive):
    body = archive.read_bytes()
    asset = {"name": "snug_archives-" + LATEST + ".tar.gz", "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
             "browser_download_url": "https://github.com/yklucz/snug/releases/download/v" + LATEST
                                     + "/snug_archives-" + LATEST + ".tar.gz",
             "state": "uploaded", "size": len(body)}
    latest = release(assets=[asset])

    def request(url, timeout):
        return io.BytesIO(json.dumps(latest).encode() if url == update.API_URL else body)

    monkeypatch.setattr(update, "_request", request)
    return latest


def assert_preserved(root):
    assert (root / "snug.py").read_text() == "old snug.py\n"
    assert (root / "runtime.json").read_text() == '{"kind":"homebrew"}'
    assert not list(root.parent.glob(f".{root.name}.update-*"))
    assert not (root.parent / f".{root.name}.update-lock").exists()


@pytest.mark.parametrize("kind", ["source", "pip", "unmarked", "foreign-marker", "wrong-root", "windows", "repair"])
def test_external_or_busy_install_refused_before_network(managed_install, monkeypatch, kind):
    root = managed_install
    if kind == "source":
        (root / ".git").mkdir()
    elif kind == "pip":
        (root / "snug_archives.egg-info").mkdir()
    elif kind == "unmarked":
        (root / update.INSTALL_MARKER).unlink()
    elif kind == "foreign-marker":
        (root / update.INSTALL_MARKER).write_text('{"schema":1,"kind":"windows","branch":"main"}')
    elif kind == "wrong-root":
        monkeypatch.setenv("SNUG_MANAGED_ROOT", str(root.parent))
    elif kind == "windows":
        monkeypatch.setattr(update.sys, "platform", "win32")
    else:
        (root / ".repair-lock").mkdir()
    with pytest.raises(update.UpdateError, match="externally|Git|pip|repair"):
        update.perform_update(CURRENT)
    assert_preserved(root)


def make_symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip('symlink creation unavailable on this host')


def test_managed_marker_cannot_be_a_symlink(managed_install):
    marker = managed_install / update.INSTALL_MARKER
    copy = marker.with_suffix(".copy")
    marker.rename(copy)
    make_symlink(marker, copy)
    with pytest.raises(update.UpdateError, match="externally"):
        update.perform_update(CURRENT)


def test_successful_update_preserves_runtime_launcher_and_shared_symlink(managed_install, tmp_path, monkeypatch):
    root = managed_install
    archive = tmp_path / "release.tar.gz"
    member = tarfile.TarInfo(f"snug_archives-{LATEST}/install.sh")
    member.size = 30
    make_sdist(archive, extra=[(member, b"must never execute this script" + b"\n")])
    provide_update(monkeypatch, archive)
    external = tmp_path / "shared Homebrew"
    external.mkdir()
    (external / "library").write_text("shared and untouched")
    make_symlink(root / "native/shared", external, directory=True)
    launcher = (root.parent / "snug-launcher").read_text()
    assert update.perform_update(CURRENT) == "Updated Snug from 1.8.0 to 1.10.2."
    assert (root / "snug.py").read_text() == 'print("snug 1.10.2")\n'
    assert not (root / "install.sh").exists()
    assert json.loads((root / update.INSTALL_MARKER).read_text())["version"] == LATEST
    assert (root / "runtime.json").read_text() == '{"kind":"homebrew"}'
    for name in ("vendor", "packages", "native"):
        assert (root / name / "keep.bin").read_bytes() == b"current runtime"
    assert (root / "native/shared").is_symlink()
    assert (external / "library").read_text() == "shared and untouched"
    assert (root.parent / "snug-launcher").read_text() == launcher
    assert not list(root.parent.glob(f".{root.name}.update-*"))
    assert not (root.parent / f".{root.name}.update-lock").exists()


def test_current_managed_install_does_not_download(managed_install, monkeypatch):
    metadata(monkeypatch, release(CURRENT))
    assert update.perform_update(CURRENT) == "Snug 1.8.0 is up to date."
    assert_preserved(managed_install)


@pytest.mark.parametrize("change", ["no-assets", "missing", "duplicate", "no-digest", "bad-digest",
                                    "untrusted-url", "http-url", "missing-state", "huge-size", "bool-size", "no-v-tag"])
def test_release_asset_requires_exact_authenticated_digest(managed_install, tmp_path, monkeypatch, change):
    archive = tmp_path / "release.tar.gz"
    make_sdist(archive)
    latest = provide_update(monkeypatch, archive)
    asset = latest["assets"][0]
    if change == "no-assets":
        latest.pop("assets")
    elif change == "missing":
        latest["assets"] = []
    elif change == "duplicate":
        latest["assets"].append(asset.copy())
    elif change == "no-digest":
        asset.pop("digest")
    elif change == "bad-digest":
        asset["digest"] = "sha1:" + "0" * 40
    elif change == "untrusted-url":
        asset["browser_download_url"] = "https://example.test/release.tar.gz"
    elif change == "http-url":
        asset["browser_download_url"] = asset["browser_download_url"].replace("https", "http")
    elif change == "missing-state":
        asset.pop("state")
    elif change == "huge-size":
        asset["size"] = update.DOWNLOAD_LIMIT + 1
    elif change == "bool-size":
        asset["size"] = True
    else:
        latest["tag_name"] = LATEST
    with pytest.raises(update.UpdateError):
        update.perform_update(CURRENT)
    assert_preserved(managed_install)


def test_new_version_check_does_not_require_install_asset(monkeypatch):
    metadata(monkeypatch, release())
    assert update.check_update(CURRENT).available


@pytest.mark.parametrize("failure", ["download", "checksum", "incomplete", "oversized", "staging", "validation"])
def test_failures_before_replacement_preserve_current(managed_install, tmp_path, monkeypatch, failure):
    archive = tmp_path / "release.tar.gz"
    make_sdist(archive)
    latest = provide_update(monkeypatch, archive)
    asset = latest["assets"][0]
    if failure == "download":
        original = update._request

        def request(url, timeout):
            if url != update.API_URL:
                raise OSError("offline")
            return original(url, timeout)

        monkeypatch.setattr(update, "_request", request)
    elif failure == "checksum":
        asset["digest"] = "sha256:" + "0" * 64
    elif failure == "incomplete":
        asset["size"] += 1
    elif failure == "oversized":
        asset["size"] -= 1
    elif failure == "staging":
        monkeypatch.setattr(update.shutil, "copytree", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    else:
        monkeypatch.setattr(update, "_validate", lambda *args: (_ for _ in ()).throw(update.UpdateError("invalid runtime")))
    with pytest.raises(update.UpdateError):
        update.perform_update(CURRENT)
    assert_preserved(managed_install)


@pytest.mark.parametrize("failure", ["old-rename", "new-rename", "post-startup", "interrupt", "post-interrupt",
                                    "old-interrupt-after-rename", "exit", "post-exit", "old-exit-after-rename"])
def test_commit_failure_restores_previous_install(managed_install, tmp_path, monkeypatch, failure):
    root = managed_install
    archive = tmp_path / "release.tar.gz"
    make_sdist(archive)
    provide_update(monkeypatch, archive)
    original_replace = update.os.replace

    def replace(source, target):
        source, target = Path(source), Path(target)
        if failure == "old-rename" and source == root:
            raise OSError("old rename failed")
        if failure in ("old-interrupt-after-rename", "old-exit-after-rename") and source == root:
            original_replace(source, target)
            if failure == "old-exit-after-rename":
                raise SystemExit(7)
            raise KeyboardInterrupt()
        if failure in ("new-rename", "interrupt", "exit") and source.name == "staged" and target == root:
            if failure == "exit":
                raise SystemExit(7)
            if failure == "interrupt":
                raise KeyboardInterrupt()
            raise OSError("new rename failed")
        return original_replace(source, target)

    monkeypatch.setattr(update.os, "replace", replace)
    if failure in ("post-startup", "post-interrupt", "post-exit"):
        original_validate = update._validate

        def validate(path, version):
            if path == root:
                if failure == "post-exit":
                    raise SystemExit(7)
                if failure == "post-interrupt":
                    raise KeyboardInterrupt()
                raise update.UpdateError("postreplacement startup failed")
            original_validate(path, version)

        monkeypatch.setattr(update, "_validate", validate)
    expected_error = update.UpdateError
    if "exit" in failure:
        expected_error = SystemExit
    elif "interrupt" in failure:
        expected_error = KeyboardInterrupt
    with pytest.raises(expected_error) as caught:
        update.perform_update(CURRENT)
    if "exit" in failure:
        assert caught.value.code == 7
    assert_preserved(root)


@pytest.mark.parametrize("rollback_error", [PermissionError, KeyboardInterrupt, SystemExit])
def test_rollback_failure_retains_only_working_backup(managed_install, tmp_path, monkeypatch, capsys, rollback_error):
    root = managed_install
    archive = tmp_path / "release.tar.gz"
    make_sdist(archive)
    provide_update(monkeypatch, archive)
    original_replace = update.os.replace
    original_validate = update._validate

    def replace(source, target):
        if Path(source).name == "previous":
            raise rollback_error("rollback rename failed")
        return original_replace(source, target)

    def validate(path, version):
        if path == root:
            raise update.UpdateError("new startup failed")
        original_validate(path, version)

    monkeypatch.setattr(update.os, "replace", replace)
    monkeypatch.setattr(update, "_validate", validate)
    expected_error = update.UpdateError if rollback_error is PermissionError else rollback_error
    with pytest.raises(expected_error) as caught:
        update.perform_update(CURRENT)
    work = list(root.parent.glob(f".{root.name}.update-*"))
    assert len(work) == 1
    backup = work[0] / "previous"
    assert (backup / "snug.py").read_text() == "old snug.py\n"
    if rollback_error is PermissionError:
        message = str(caught.value)
    else:
        message = capsys.readouterr().err
    assert "previous installation is preserved at" in message
    assert str(backup) in message
    assert "Restore that directory to the original installation path before retrying" in message
    assert not (root.parent / f".{root.name}.update-lock").exists()


def test_concurrent_update_lock_is_preserved(managed_install):
    lock = managed_install.parent / f".{managed_install.name}.update-lock"
    lock.mkdir()
    with pytest.raises(update.UpdateError, match="Another Snug update"):
        update.perform_update(CURRENT)
    assert lock.is_dir()
    assert (managed_install / "snug.py").read_text() == "old snug.py\n"


@pytest.mark.parametrize("kind", ["traversal", "absolute", "backslash", "colon", "empty-component", "dot-component",
                                  "other-root", "duplicate", "case-duplicate", "symlink", "hardlink", "device"])
def test_entire_tar_is_validated_even_excluded_members(managed_install, tmp_path, monkeypatch, kind):
    archive = tmp_path / "release.tar.gz"
    base = f"snug_archives-{LATEST}"
    names = {"traversal": f"{base}/docs/../../outside", "absolute": f"/{base}/outside",
             "backslash": f"{base}/docs\\outside", "colon": f"{base}/C:outside",
             "empty-component": f"{base}//outside", "dot-component": f"{base}/./outside",
             "other-root": "another-project/docs/file", "duplicate": f"{base}/snug.py",
             "case-duplicate": f"{base}/SNUG.PY"}
    member = tarfile.TarInfo(names.get(kind, f"{base}/docs/unsafe"))
    if kind == "symlink":
        member.type = tarfile.SYMTYPE
        member.linkname = "/outside"
    elif kind == "hardlink":
        member.type = tarfile.LNKTYPE
        member.linkname = f"{base}/snug.py"
    elif kind == "device":
        member.type = tarfile.CHRTYPE
    else:
        member.size = 4
    make_sdist(archive, extra=[(member, None if kind in ("symlink", "hardlink", "device") else b"evil")])
    provide_update(monkeypatch, archive)
    with pytest.raises(update.UpdateError, match="unsafe|duplicate|unsupported"):
        update.perform_update(CURRENT)
    assert_preserved(managed_install)


def test_missing_owned_file_rejected_before_replacement(managed_install, tmp_path, monkeypatch):
    archive = tmp_path / "release.tar.gz"
    make_sdist(archive, missing=["snug_update.py"])
    provide_update(monkeypatch, archive)
    with pytest.raises(update.UpdateError, match="missing required"):
        update.perform_update(CURRENT)
    assert_preserved(managed_install)


def test_decompression_bomb_is_bounded(tmp_path, monkeypatch):
    archive = tmp_path / "bomb.gz"
    archive.write_bytes(gzip.compress(b"\0" * 10000))
    monkeypatch.setattr(update, "UNPACKED_LIMIT", 1000)
    with pytest.raises(update.UpdateError, match="unpacked size"):
        update._release_files(archive, LATEST)


def test_corrupt_gzip_data_has_actionable_error(tmp_path):
    archive = tmp_path / "corrupt.gz"
    archive.write_bytes(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + b"\xff" * 20)
    with pytest.raises(update.UpdateError, match="invalid or unreadable"):
        update._release_files(archive, LATEST)


def test_truncated_chunked_asset_download_preserves_current(managed_install, tmp_path, monkeypatch):
    archive = tmp_path / "release.tar.gz"
    make_sdist(archive)
    provide_update(monkeypatch, archive)
    original_request = update._request

    def request(url, timeout):
        if url == update.API_URL:
            return original_request(url, timeout)
        raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(update, "_request", request)
    with pytest.raises(update.UpdateError, match="network unavailable"):
        update.perform_update(CURRENT)
    assert_preserved(managed_install)


def test_deeply_nested_install_marker_is_rejected(managed_install):
    (managed_install / update.INSTALL_MARKER).write_text("[" * 1500 + "0" + "]" * 1500)
    with pytest.raises(update.UpdateError, match="externally"):
        update.perform_update(CURRENT)


def test_staging_does_not_follow_a_copied_marker_symlink(managed_install, tmp_path):
    external = tmp_path / "shared-marker.json"
    external.write_text("shared file must stay untouched")
    marker = managed_install / update.INSTALL_MARKER
    marker.unlink()
    make_symlink(marker, external)
    staged = tmp_path / "stage"
    update._stage(managed_install, staged, {}, LATEST)
    assert external.read_text() == "shared file must stay untouched"
    assert not (staged / update.INSTALL_MARKER).is_symlink()
    assert json.loads((staged / update.INSTALL_MARKER).read_text())["version"] == LATEST


def test_continuously_streaming_asset_has_total_deadline(tmp_path, monkeypatch):
    body = b"verified artifact"
    monkeypatch.setattr(update, "_request", lambda *args: io.BytesIO(body))
    clock = iter([0.0, 0.0, update.DOWNLOAD_TIMEOUT + 1.0])
    monkeypatch.setattr(update.time, "monotonic", lambda: next(clock))
    with pytest.raises(update.UpdateError, match="download timed out"):
        update._download("https://github.com/yklucz/snug/releases/download/v1.10.2/file", hashlib.sha256(body).hexdigest(),
                         len(body), tmp_path / "download.tar.gz")


def test_validation_uses_argument_arrays_and_never_repairs(managed_install, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=f"snug {LATEST}\n")

    monkeypatch.setattr(update.subprocess, "run", run)
    update._validate(managed_install, LATEST)
    assert calls[0][0] == [update.sys.executable, "-B", str(managed_install / "snug.py"), "--version"]
    assert calls[1][0] == [update.sys.executable, "-B", str(managed_install / "snug_runtime.py"), "--check", "all"]
    for command, kwargs in calls:
        assert isinstance(command, list) and "shell" not in kwargs
        assert kwargs["cwd"] == managed_install
        assert kwargs["env"]["SNUG_NO_UPDATE_CHECK"] == "1"
        assert kwargs["env"]["SNUG_PACKAGES"] == str(managed_install / "packages")
        assert "PYTHONPATH" not in kwargs["env"]


@pytest.mark.parametrize("failure", ["version", "backend", "timeout", "spawn", "encoding"])
def test_validation_failures_are_actionable(managed_install, monkeypatch, failure):
    def run(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        if failure == "spawn":
            raise OSError("bad interpreter")
        if failure == "encoding":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid CLI output")
        if "--version" in command:
            return SimpleNamespace(returncode=0, stdout="snug 0.0.0" if failure == "version" else f"snug {LATEST}")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(update.subprocess, "run", run)
    with pytest.raises(update.UpdateError, match="validation|startup"):
        update._validate(managed_install, LATEST)
