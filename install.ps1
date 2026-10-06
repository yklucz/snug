$ErrorActionPreference = 'Stop'
# Prefer built-in modules when invoked through a process from another PowerShell.
$BuiltinModules = [IO.Path]::Combine($PSHOME, 'Modules')
$env:PSModulePath = $BuiltinModules + [IO.Path]::PathSeparator + $env:PSModulePath
$RawBase = if ($env:SNUG_RAW_BASE) { $env:SNUG_RAW_BASE } else { 'https://raw.githubusercontent.com/yklucz/snug/main' }
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Snug'
$Staging = Join-Path $env:LOCALAPPDATA ('.snug-install-' + [Guid]::NewGuid().ToString('N'))
$Backup = $InstallRoot + '.previous'

[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
New-Item -ItemType Directory -Path $Staging | Out-Null
try {
    Write-Host '==> Downloading Snug'
    foreach ($Source in @('snug.py', 'snug_core.py', 'snug_ext.py', 'snug_runtime.py', 'runtime.ps1', 'runtime-lock.json')) {
        Invoke-WebRequest -UseBasicParsing -Uri "$RawBase/$Source" -OutFile (Join-Path $Staging $Source)
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Staging 'runtime.ps1') -PrepareOnly
    if ($LASTEXITCODE -ne 0) { throw 'The staged installation failed validation; the existing installation was kept.' }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Staging 'runtime.ps1') --version
    if ($LASTEXITCODE -ne 0) { throw 'The staged CLI failed validation; the existing installation was kept.' }
    if (Test-Path -LiteralPath $Backup) { throw 'An earlier installation backup exists; move it aside before reinstalling.' }
    if (Test-Path -LiteralPath $InstallRoot) { Move-Item -LiteralPath $InstallRoot -Destination $Backup }
    try { Move-Item -LiteralPath $Staging -Destination $InstallRoot } catch {
        if (Test-Path -LiteralPath $Backup) { Move-Item -LiteralPath $Backup -Destination $InstallRoot }
        throw
    }
    $BinDir = Join-Path $InstallRoot 'bin'
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    $Launcher = Join-Path $BinDir 'snug.cmd'
    # Relative paths support Unicode usernames and survive moving the staged app.
    Set-Content -LiteralPath $Launcher -Encoding ASCII -Value '@echo off', 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\runtime.ps1" %*', 'exit /b %ERRORLEVEL%'
    & $Launcher --version
    if ($LASTEXITCODE -ne 0) {
        Move-Item -LiteralPath $InstallRoot -Destination $Staging
        if (Test-Path -LiteralPath $Backup) { Move-Item -LiteralPath $Backup -Destination $InstallRoot }
        throw 'Snug failed its startup check; the previous installation was restored.'
    }
    if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Recurse -Force }
} finally {
    if (Test-Path -LiteralPath $Staging) { Remove-Item -LiteralPath $Staging -Recurse -Force }
}

$UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$Parts = if ($UserPath) { $UserPath -split ';' } else { @() }
if (-not ($Parts | Where-Object { $_.TrimEnd('\') -eq $BinDir.TrimEnd('\') })) {
    $NewPath = if ($UserPath) { "$UserPath;$BinDir" } else { $BinDir }
    [Environment]::SetEnvironmentVariable('Path', $NewPath, 'User')
}
$env:Path = "$env:Path;$BinDir"
Write-Host '==> Snug installed. Run: snug'
