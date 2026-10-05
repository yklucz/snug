$ErrorActionPreference = "Stop"

$RawUrl = "https://raw.githubusercontent.com/yklucz/snug/main/snug.py"
$InstallRoot = Join-Path $env:LOCALAPPDATA "Snug"
$BinDir = Join-Path $InstallRoot "bin"
$AppPath = Join-Path $InstallRoot "snug.py"
$Launcher = Join-Path $BinDir "snug.cmd"

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $Python) { throw "Python 3.10+ is required." }

& $Python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10+ is required." }

New-Item -ItemType Directory -Force -Path $InstallRoot, $BinDir | Out-Null

Write-Host "==> Downloading Snug"
Invoke-WebRequest -UseBasicParsing -Uri $RawUrl -OutFile $AppPath
& $Python.Source -m py_compile $AppPath
if ($LASTEXITCODE -ne 0) { throw "Downloaded source failed syntax check." }

$LauncherContent = "@echo off`r`n`"" + $Python.Source + "`" `"" + $AppPath + "`" %*`r`n"
Set-Content -Path $Launcher -Value $LauncherContent -Encoding ASCII

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$Parts = @()
if ($UserPath) { $Parts = $UserPath -split ";" }
if (-not ($Parts | Where-Object { $_.TrimEnd("\") -eq $BinDir.TrimEnd("\") })) {
    $NewPath = if ($UserPath) { "$UserPath;$BinDir" } else { $BinDir }
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
}

if (-not (($env:Path -split ";") | Where-Object { $_.TrimEnd("\") -eq $BinDir.TrimEnd("\") })) {
    $env:Path = "$env:Path;$BinDir"
}

& $Launcher --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Snug failed its startup check." }

Write-Host "==> Snug installed."
Write-Host "Run: snug"