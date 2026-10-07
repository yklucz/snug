"""Run on Windows CI, including the no-existing-Python installation path."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

windows_host = pytest.mark.skipif(os.name != "nt", reason="requires a Windows host")
ROOT = Path(__file__).resolve().parents[1]


def test_diagnostics_launcher_bypasses_dependency_repair():
    source = (ROOT / "runtime.ps1").read_text(encoding="utf-8")
    diagnostic = source.index("$SnugArguments[0] -in @('doctor', 'formats')")
    dependency_check = source.index("if (-not (Test-Dependencies $Python))", diagnostic)
    bypass = source[diagnostic:dependency_check]
    assert "Offline diagnostics require an existing compatible Python runtime" in bypass
    assert "exit (Invoke-Python $Python $RunArguments)" in bypass
    assert "Install-Python" not in bypass and "--windows-repair" not in bypass


@windows_host
@pytest.mark.parametrize("command", ["doctor", "formats"])
def test_windows_diagnostics_skip_corrupt_state_and_broken_dependencies(tmp_path, command):
    app = tmp_path / "Snug diagnostics Tiếng Việt 📦 author's (app)"
    app.mkdir()
    for name in ("snug.py", "snug_core.py", "snug_ext.py", "snug_runtime.py", "snug_update.py", "runtime.ps1", "runtime-lock.json"):
        shutil.copyfile(ROOT / name, app / name)
    (app / "runtime.json").write_text("[]", encoding="utf-8")
    # Exercise the real diagnostic command with broken app-owned optional
    # components. Fail explicitly if it probes, repairs, or uses the network.
    runtime = app / "snug_runtime.py"
    guard = '''\ndef offline_forbidden(*args, **kwargs):
    raise AssertionError("Offline diagnostics called a probe, repair, or network")
check = windows_repair = download = offline_forbidden
urllib.request.urlopen = offline_forbidden
'''
    source = runtime.read_text(encoding="utf-8")
    runtime.write_text(source.replace('if __name__ == "__main__":', guard + '\nif __name__ == "__main__":'), encoding="utf-8")
    for name, error in (("vendor/libarchive", "OSError('broken fixture DLL')"),
                        ("packages/py7zr", "ImportError('missing fixture py7zr')")):
        directory = app / name
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("raise " + error + "\n", encoding="utf-8")
    system = Path(os.environ["SystemRoot"]) / "System32"
    powershell = system / "WindowsPowerShell/v1.0/powershell.exe"
    home = tmp_path / "local app data"
    env = dict(os.environ, PATH=os.pathsep.join((str(Path(sys.executable).parent), str(system))),
               LOCALAPPDATA=str(home), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    for name in ("SNUG_LIBRARY", "LIBARCHIVE", "SNUG_PACKAGES", "SNUG_NO_UPDATE_CHECK", "PYTHONPATH"):
        env.pop(name, None)
    before = {path.relative_to(app): path.read_bytes() for path in app.rglob("*") if path.is_file()}
    result = subprocess.run([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(app / "runtime.ps1"), command, "--json"], env=env,
                            capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == (2 if command == "doctor" else 0), result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1 and not result.stderr
    if command == "doctor":
        assert report["runtime"]["state"]["status"] == "corrupt"
        assert report["ok"] is False
    after = {path.relative_to(app): path.read_bytes() for path in app.rglob("*") if path.is_file()}
    assert before == after and not list(home.rglob("*"))


@windows_host
@pytest.mark.parametrize("command", ["doctor", "formats"])
def test_windows_diagnostics_without_python_do_not_install(tmp_path, command):
    app = tmp_path / "Snug without Python"
    app.mkdir()
    shutil.copyfile(ROOT / "runtime.ps1", app / "runtime.ps1")
    system = Path(os.environ["SystemRoot"]) / "System32"
    powershell = system / "WindowsPowerShell/v1.0/powershell.exe"
    before = (app / "runtime.ps1").read_bytes()
    result = subprocess.run([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(app / "runtime.ps1"), command, "--json"], env=dict(os.environ, PATH=str(system)),
                            capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 1 and not result.stdout
    assert "Offline diagnostics require an existing compatible Python runtime" in result.stderr
    assert "No installation or repair was attempted" in result.stderr
    assert list(app.iterdir()) == [app / "runtime.ps1"]
    assert (app / "runtime.ps1").read_bytes() == before


@windows_host
@pytest.mark.parametrize('script_name', ['install.ps1', 'runtime.ps1'])
def test_windows_downloads_reject_downgrades_before_following(tmp_path, script_name):
    # Extract only the helper, leaving installation/repair untouched. Custom
    # WebRequest prefixes provide redirect responses without any network I/O.
    probe = tmp_path / 'download-probe.ps1'
    probe.write_text(r'''param([string]$ScriptPath, [string]$Destination)
$ErrorActionPreference = 'Stop'
$Tokens = $null
$Errors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$Tokens, [ref]$Errors)
if ($Errors.Count) { throw 'PowerShell script contains syntax errors.' }
$Function = $Ast.Find({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq 'Save-HttpsFile'
}, $true)
if (-not $Function) { throw 'HTTPS download helper was not found.' }
Invoke-Expression $Function.Extent.Text
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Net;
using System.Text;
public class OfflineCreator : IWebRequestCreate {
    public WebRequest Create(Uri uri) { return new OfflineRequest(); }
}
public class OfflineRequest : WebRequest {
    public static int Calls;
    public static string Mode;
    public bool AllowAutoRedirect { get; set; }
    public override int Timeout { get; set; }
    public int ReadWriteTimeout { get; set; }
    public override WebResponse GetResponse() {
        Calls++;
        bool redirect = Calls == 1 || Mode == "loop";
        return new OfflineResponse(redirect, Mode == "downgrade");
    }
}
public class OfflineResponse : WebResponse {
    public HttpStatusCode StatusCode { get; private set; }
    private WebHeaderCollection headers = new WebHeaderCollection();
    public override WebHeaderCollection Headers { get { return headers; } }
    public OfflineResponse(bool redirect, bool downgrade) {
        StatusCode = redirect ? HttpStatusCode.Redirect : HttpStatusCode.OK;
        if (redirect) Headers["Location"] = (downgrade ? "http" : "https") + "://snug-download.invalid/verified";
    }
    public override Stream GetResponseStream() {
        return new MemoryStream(Encoding.UTF8.GetBytes("verified artifact"));
    }
}
'@
[void][Net.WebRequest]::RegisterPrefix('https://snug-download.invalid/', [OfflineCreator]::new())
[void][Net.WebRequest]::RegisterPrefix('http://snug-download.invalid/', [OfflineCreator]::new())
[OfflineRequest]::Calls = 0
try { Save-HttpsFile 'http://snug-download.invalid/artifact' $Destination; throw 'Accepted initial HTTP URL.' }
catch { if ($_.Exception.Message -notmatch 'require HTTPS') { throw } }
if ([OfflineRequest]::Calls -ne 0 -or (Test-Path $Destination)) { throw 'Initial HTTP made a request or wrote a file.' }
[OfflineRequest]::Mode = 'downgrade'
try { Save-HttpsFile 'https://snug-download.invalid/artifact' $Destination; throw 'Accepted redirect downgrade.' }
catch { if ($_.Exception.Message -notmatch 'require HTTPS') { throw } }
if ([OfflineRequest]::Calls -ne 1 -or (Test-Path $Destination)) { throw 'Downgrade made a request or wrote a file.' }
[OfflineRequest]::Calls = 0
[OfflineRequest]::Mode = 'https'
Save-HttpsFile 'https://snug-download.invalid/artifact' $Destination
if ([OfflineRequest]::Calls -ne 2 -or (Get-Content -Raw $Destination) -ne 'verified artifact') { throw 'HTTPS redirect failed.' }
Remove-Item -LiteralPath $Destination
[OfflineRequest]::Calls = 0
[OfflineRequest]::Mode = 'loop'
try { Save-HttpsFile 'https://snug-download.invalid/artifact' $Destination; throw 'Accepted unlimited redirects.' }
catch { if ($_.Exception.Message -notmatch 'redirect limit') { throw } }
if ([OfflineRequest]::Calls -ne 11 -or (Test-Path $Destination)) { throw 'Redirect limit was not enforced before writing.' }
''', encoding='utf-8')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(powershell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(probe),
                             str(ROOT / script_name), str(tmp_path / 'artifact')],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@windows_host
@pytest.mark.skipif(os.environ.get("SNUG_WINDOWS_LIVE_BOOTSTRAP") != "1",
                   reason="set SNUG_WINDOWS_LIVE_BOOTSTRAP=1 for live runtime downloads")
def test_windows_bootstrap_password_workflow_and_deleted_dll_repair(tmp_path):
    app = tmp_path / "Snug runtime Tiếng Việt 📦 author's (app)"
    app.mkdir()
    for name in ("snug.py", "snug_core.py", "snug_ext.py", "snug_runtime.py", "snug_update.py", "runtime.ps1", "runtime-lock.json"):
        shutil.copyfile(ROOT / name, app / name)
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = dict(os.environ)
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    # Reproduce an intermediate Python process inheriting a module search
    # path without Windows PowerShell's built-ins (as with PowerShell 7).
    env["PSMODULEPATH"] = str(tmp_path / "PowerShell7Modules")
    env["SNUG_NO_UPDATE_CHECK"] = "1"
    env["LOCALAPPDATA"] = str(tmp_path / "local app data")
    env["PYTHONIOENCODING"] = "utf-8"
    for key in ("LIBARCHIVE", "SNUG_LIBRARY", "SNUG_PACKAGES"):
        env.pop(key, None)
    def run(*args):
        return subprocess.run([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(app / "runtime.ps1"), *map(str, args)], env=env,
                              capture_output=True, encoding="utf-8", timeout=240)
    version = run("--version")
    assert version.returncode == 0, version.stderr
    assert "snug 1.8.0" in version.stdout
    assert (app / "python/python.exe").is_file()
    assert not list(app.rglob("pip"))
    assert not list(app.glob(".repair-*"))
    source = tmp_path / "Tài liệu author's 📦 (notes) [final].txt"
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
    disabled = run("update", "--disable-checks")
    assert disabled.returncode == 0, disabled.stderr
    state = tmp_path / "local app data/Snug/state/update.json"
    assert state.is_file()
    assert json.loads(state.read_text())["automatic_checks"] is False
    enabled = run("update", "--enable-checks")
    assert enabled.returncode == 0, enabled.stderr
    assert json.loads(state.read_text())["automatic_checks"] is True
    unsupported = run("update")
    assert unsupported.returncode == 2
    assert "managed externally" in unsupported.stderr
    assert "Traceback" not in unsupported.stderr
