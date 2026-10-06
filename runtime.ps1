param(
    [switch]$PrepareOnly,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$SnugArguments
)
$ErrorActionPreference = 'Stop'
# A Python/cmd child of PowerShell 7 can inherit incompatible module paths.
# Keep this interpreter's built-in cmdlets ahead of inherited modules.
$BuiltinModules = [IO.Path]::Combine($PSHOME, 'Modules')
$env:PSModulePath = $BuiltinModules + [IO.Path]::PathSeparator + $env:PSModulePath
$Root = $PSScriptRoot

function Save-HttpsFile([Uri]$Uri, [string]$Destination) {
    # Follow redirects ourselves so Windows PowerShell 5.1 cannot downgrade TLS.
    for ($Redirects = 0; $Redirects -le 10; $Redirects++) {
        if ($Uri.Scheme -ne 'https') { throw 'Snug downloads and redirects require HTTPS.' }
        $Request = [Net.WebRequest]::Create($Uri)
        $Request.AllowAutoRedirect = $false
        $Request.Timeout = 60000
        $Request.ReadWriteTimeout = 60000
        $Response = $Request.GetResponse()
        try {
            $Status = [int]$Response.StatusCode
            if ($Status -in @(301, 302, 303, 307, 308)) {
                if ($Redirects -eq 10) { throw 'Snug download exceeded its redirect limit.' }
                $Location = $Response.Headers['Location']
                if (-not $Location) { throw 'Snug download redirect has no destination.' }
                $Uri = [Uri]::new($Uri, $Location)
                continue
            }
            if ($Status -lt 200 -or $Status -ge 300) { throw "Snug download failed with HTTP status $Status." }
            $Source = $Response.GetResponseStream()
            try {
                $Output = [IO.File]::Open($Destination, [IO.FileMode]::Create)
                try { $Source.CopyTo($Output) } finally { $Output.Dispose() }
            } finally { $Source.Dispose() }
            return
        } finally { $Response.Dispose() }
    }
}

function Quote-NativeArgument([string]$Value) {
    # Windows CommandLineToArgvW quoting, including trailing backslashes.
    $Escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $Escaped = [regex]::Replace($Escaped, '\\+$', '$0$0')
    return '"' + $Escaped + '"'
}

function Invoke-Python([string]$Executable, [string[]]$Arguments, [switch]$Quiet) {
    $Start = New-Object Diagnostics.ProcessStartInfo
    $Start.FileName = $Executable
    $Start.Arguments = ($Arguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
    $Start.UseShellExecute = $false
    $Start.RedirectStandardOutput = $Quiet.IsPresent
    $Start.RedirectStandardError = $Quiet.IsPresent
    $Process = [Diagnostics.Process]::Start($Start)
    if ($Quiet) {
        $Output = $Process.StandardOutput.ReadToEndAsync()
        $Errors = $Process.StandardError.ReadToEndAsync()
    }
    $Process.WaitForExit()
    if ($Quiet) { $Output.Wait(); $Errors.Wait() }
    $Code = $Process.ExitCode
    $Process.Dispose()
    return $Code
}

function Test-Python([string]$Executable) {
    if (-not $Executable -or -not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    try {
        $Code = Invoke-Python $Executable @('-B', '-c', 'import sys, sysconfig; assert sys.implementation.name == "cpython" and (3,10) <= sys.version_info < (3,15) and sysconfig.get_platform() == "win-amd64" and not sysconfig.get_config_var("Py_GIL_DISABLED")') -Quiet
        return $Code -eq 0
    } catch { return $false }
}

function Find-Python {
    $Candidates = @((Join-Path $Root 'python\python.exe'))
    $StatePath = Join-Path $Root 'runtime.json'
    if (Test-Path -LiteralPath $StatePath) {
        try { $Candidates += (Get-Content -Raw $StatePath | ConvertFrom-Json).python } catch { }
    }
    foreach ($Name in @('python', 'python3')) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command -and $Command.Source -notlike '*\WindowsApps\*') { $Candidates += $Command.Source }
    }
    # The Python launcher may know interpreters that are not on PATH.
    $Py = Get-Command py -ErrorAction SilentlyContinue
    if ($Py) {
        try { $Candidates += (& $Py.Source -3 -c 'import sys; print(sys.executable)' 2>$null) } catch { }
    }
    foreach ($Candidate in $Candidates) { if (Test-Python $Candidate) { return $Candidate } }
    return $null
}

function Install-Python {
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'The managed Windows installer requires 64-bit Windows 10 or 11.' }
    $Artifact = (Get-Content -Raw (Join-Path $Root 'runtime-lock.json') | ConvertFrom-Json).windows.python
    $Temporary = Join-Path $Root ('.python-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $Temporary | Out-Null
    try {
        $Zip = Join-Path $Temporary 'python.zip'
        Save-HttpsFile ([Uri]$Artifact.url) $Zip
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $Zip).Hash.ToLowerInvariant() -ne $Artifact.sha256) { throw 'Python download checksum mismatch.' }
        $Staged = Join-Path $Temporary 'python'
        Expand-Archive -LiteralPath $Zip -DestinationPath $Staged
        Set-Content -LiteralPath (Join-Path $Staged 'python314._pth') -Encoding ASCII -Value @('python314.zip', '.', '..', 'import site')
        $Candidate = Join-Path $Staged 'python.exe'
        if (-not (Test-Python $Candidate)) { throw 'The downloaded Python runtime failed validation.' }
        $Destination = Join-Path $Root 'python'
        $Backup = Join-Path $Temporary 'previous'
        if (Test-Path -LiteralPath $Destination) { Move-Item -LiteralPath $Destination -Destination $Backup }
        try { Move-Item -LiteralPath $Staged -Destination $Destination } catch {
            if (Test-Path -LiteralPath $Backup) { Move-Item -LiteralPath $Backup -Destination $Destination }
            throw
        }
        return (Join-Path $Destination 'python.exe')
    } finally { Remove-Item -LiteralPath $Temporary -Recurse -Force }
}

function Test-Dependencies([string]$Python) {
    if (-not $Python) { return $false }
    return (Invoke-Python $Python @('-B', (Join-Path $Root 'snug_runtime.py'), '--check') -Quiet) -eq 0
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $Python = Find-Python
    if (-not (Test-Dependencies $Python)) {
        $Hash = [Security.Cryptography.SHA256]::Create()
        $Key = [BitConverter]::ToString($Hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant()))).Replace('-', '')
        $Hash.Dispose()
        $Mutex = New-Object Threading.Mutex($false, ('Local\SnugRepair-' + $Key))
        $Owned = $false
        try {
            try { $Owned = $Mutex.WaitOne(60000) } catch [Threading.AbandonedMutexException] { $Owned = $true }
            if (-not $Owned) { throw 'Another Snug dependency repair is still running. Try again when it finishes.' }
            $Python = Find-Python
            if (-not $Python) { Write-Host '==> Installing minimal Python runtime'; $Python = Install-Python }
            if (-not (Test-Dependencies $Python)) {
                Write-Host '==> Installing or repairing archive dependencies'
                if ((Invoke-Python $Python @('-B', (Join-Path $Root 'snug_runtime.py'), '--windows-repair')) -ne 0) { throw 'Dependency repair failed. Check your connection and run snug again.' }
                if (-not (Test-Dependencies $Python)) { throw 'Repaired dependencies failed validation.' }
                [void](Invoke-Python $Python @('-B', (Join-Path $Root 'snug_runtime.py'), '--storage'))
            }
        } finally {
            if ($Owned) { $Mutex.ReleaseMutex() }
            $Mutex.Dispose()
        }
    }
    if ($PrepareOnly) { exit 0 }
    $RunArguments = @('-B', (Join-Path $Root 'snug_runtime.py'), '--run') + $SnugArguments
    exit (Invoke-Python $Python $RunArguments)
} catch {
    [Console]::Error.WriteLine('Snug dependency error: ' + $_.Exception.Message)
    exit 1
}
