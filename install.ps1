$ErrorActionPreference = "Stop"

$RawBase = "https://raw.githubusercontent.com/yklucz/snug/main"
$InstallRoot = Join-Path $env:LOCALAPPDATA "Snug"
$BinDir = Join-Path $InstallRoot "bin"
$Launcher = Join-Path $BinDir "snug.cmd"
$Staging = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString())

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $Python) { throw "Python 3.10+ is required." }
& $Python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10+ is required." }

New-Item -ItemType Directory -Force -Path $InstallRoot, $BinDir, $Staging | Out-Null
try {
    Write-Host "==> Downloading Snug"
    foreach ($Source in @("snug.py", "snug_core.py", "snug_ext.py")) {
        $Target = Join-Path $Staging $Source
        Invoke-WebRequest -UseBasicParsing -Uri "$RawBase/$Source" -OutFile $Target
        & $Python.Source -m py_compile $Target
        if ($LASTEXITCODE -ne 0) { throw "Downloaded source failed syntax check." }
    }

    & $Python.Source -m venv (Join-Path $InstallRoot "venv")
    if ($LASTEXITCODE -ne 0) { throw "Python venv support is required." }
    $AppPython = Join-Path $InstallRoot "venv\Scripts\python.exe"
    & $AppPython -m pip install --disable-pip-version-check --only-binary=:all: "libarchive-c>=5.3,<6" "py7zr>=1.1.3,<2"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Optional backends could not be installed. Native ZIP/TAR and compression streams remain available."
        Write-Host "Retry: & '$AppPython' -m pip install --only-binary=:all: 'libarchive-c>=5.3,<6' 'py7zr>=1.1.3,<2'"
    }
    & $AppPython (Join-Path $Staging "snug.py") --version | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Downloaded Snug failed its startup check." }
    foreach ($Source in @("snug.py", "snug_core.py", "snug_ext.py")) {
        Move-Item -Force (Join-Path $Staging $Source) (Join-Path $InstallRoot $Source)
    }
    $AppPath = Join-Path $InstallRoot "snug.py"
    $LauncherContent = "@echo off`r`n`"" + $AppPython + "`" `"" + $AppPath + "`" %*`r`n"
    Set-Content -Path $Launcher -Value $LauncherContent -Encoding ASCII
} finally {
    Remove-Item -Recurse -Force $Staging
}

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
& $Launcher --version
if ($LASTEXITCODE -ne 0) { throw "Snug failed its startup check." }
& $AppPython -c "import sys; sys.path.insert(0, sys.argv[1]); from snug_ext import LibarchiveBackend; raise SystemExit(0 if LibarchiveBackend().available() else 1)" $InstallRoot
if ($LASTEXITCODE -ne 0) {
    Write-Host "libarchive support unavailable. Install a prebuilt libarchive DLL (for example with conda-forge or MSYS2)."
    Write-Host 'Set LIBARCHIVE to its full DLL path, then restart the terminal. Visual Studio is not required.'
}
Write-Host "==> Snug installed."
Write-Host "Run: snug"
