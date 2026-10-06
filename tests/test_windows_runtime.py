"""Run on Windows CI, including the no-existing-Python installation path."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires a Windows host")
ROOT = Path(__file__).resolve().parents[1]


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


def test_windows_bootstrap_password_workflow_and_deleted_dll_repair(tmp_path):
    app = tmp_path / "Snug runtime with spaces"
    app.mkdir()
    for name in ("snug.py", "snug_core.py", "snug_ext.py", "snug_runtime.py", "runtime.ps1", "runtime-lock.json"):
        shutil.copyfile(ROOT / name, app / name)
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = dict(os.environ)
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    # Reproduce an intermediate Python process inheriting a module search
    # path without Windows PowerShell's built-ins (as with PowerShell 7).
    env["PSMODULEPATH"] = str(tmp_path / "PowerShell7Modules")
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
