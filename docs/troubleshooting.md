# Troubleshooting

[← Back to README](../README.md)

First distinguish a managed launcher from a source/pip command. Managed errors begin with `Snug dependency error:` and concern startup checks or repair. Source CLI errors generally begin with `error:`. See [Installation](installation.md) for setup and [Runtime management](runtime.md) for repair behavior.

## `snug: command not found` and Windows PATH

On macOS/Linux, the default launcher is `~/.local/bin/snug`. Check it directly, then add its directory to your shell profile:

```bash
"$HOME/.local/bin/snug" --version
export PATH="$HOME/.local/bin:$PATH"
```

Use the actual prefix if you set `SNUG_PREFIX`. Open a new terminal after editing the profile.

On Windows, run the installed command directly and refresh the current session's path if necessary:

```powershell
& "$env:LOCALAPPDATA\Snug\bin\snug.cmd" --version
$env:Path += ";$env:LOCALAPPDATA\Snug\bin"
snug --version
```

The installer persists this path in the user environment, but already open terminals retain their previous path. For pip installations, activate the correct environment or run `.venv/bin/snug` on Unix and `.\.venv\Scripts\snug.exe` on Windows. `python snug.py --version` also works from a source checkout.

## Unsupported Python

The package requires Python 3.10+. Check the interpreter associated with the source environment:

```bash
python --version
python -m pip --version
```

Create a new environment with a supported Python if necessary; changing a system `python` command does not change an existing virtual environment. Managed checks can report `Python 3.10+ is required`.

The Windows managed launcher requires standard x64 CPython 3.10–3.14, excluding free-threaded builds. Other interpreters may work for source installs but are not accepted by that launcher. It downloads the fallback runtime when no accepted interpreter is found. Repair can report `managed Windows dependencies require CPython 3.10-3.14 x64` or `no locked Windows wheel for Python 3.X` if called with an unsupported interpreter. See [Windows installation](installation.md#windows), including ARM64 behavior.

## Homebrew missing

Actual launcher messages include `Please install Homebrew (Linuxbrew on Linux) from https://brew.sh, then run the installer or snug again.` and `Homebrew executable not found.`

Install Homebrew, restore its executable to `PATH`, or point to an existing installation:

```bash
export SNUG_BREW="/opt/homebrew/bin/brew"
snug --version
```

Use your actual path (`/usr/local/bin/brew` or `/home/linuxbrew/.linuxbrew/bin/brew` are other searched locations). The Unix managed installer requires Homebrew even when system Python/libarchive exists. Choose a source installation to use distribution packages instead.

## libarchive or its DLL cannot load

Errors may say `libarchive support requires libarchive-c and the native libarchive library`, `FORMAT support requires the libarchive backend (install snug-archives[extended])`, or, in managed startup, `Snug's native libraries need repair` / `managed installations require libarchive 3.8+`.

For source installations, install both `.[extended]` and the native library using [platform instructions](installation.md#native-libarchive-for-source-installations). Check the binding with the same interpreter that runs Snug:

```bash
python -c "import libarchive; print(libarchive.ffi.version_number())"
```

Set `LIBARCHIVE` to an actual full library path before Python starts if automatic discovery fails. For example, with macOS Homebrew:

```bash
export LIBARCHIVE="$(brew --prefix libarchive)/lib/libarchive.dylib"
snug info archive.rar
```

For Windows source installs, ensure the main DLL and all dependent DLLs exist and match Python's architecture:

```powershell
$env:LIBARCHIVE = 'C:\tools\libarchive\libarchive-13.dll'
$env:Path = 'C:\tools\libarchive;' + $env:Path
python -c "import snug_ext; snug_ext.LibarchiveBackend._library(); print('libarchive loaded')"
```

Use your actual directory and DLL filename. An error naming the main DLL can also mean a dependent DLL is missing. Restart the Python process after changing library discovery settings. For managed installations, reconnect and run Snug again so the launcher can repair; if application scripts or the lock are missing, rerun the installer.

## Missing py7zr or unavailable backend

Actual errors include `7z support requires py7zr (install Snug with the 7z extra)` and `7z creation requires the py7zr backend (install snug-archives[7z])`.

From a source checkout, install the extra in the same environment:

```bash
python -m pip install '.[7z]'
snug info backup.7z
```

libarchive provides fallback 7z extraction when py7zr is unavailable, but 7z creation requires py7zr. For managed startup errors such as `py7zr 1.1.3 through 1.x is required` or `py7zr is missing its streaming writer API`, reconnect and rerun the managed command to repair. Installing into an unrelated system Python does not fix Homebrew's selected interpreter or application-owned Windows packages.

## Unsupported format or codec

Errors can include `cannot determine archive format`, `the installed backend cannot read this FORMAT archive or its compression method`, `FORMAT creation is not supported; choose a writable format`, or a more specific decoder error.

Check the [format matrix and limits](supported-formats.md). Missing backends require installation; unsupported codecs require a suitable native build or conversion with a tool that supports the original archive. Use `--format` only for creation with an unknown output suffix; extraction has no format override. Renaming an unsupported archive does not add a decoder. A header that is detected successfully may still be truncated or corrupt.

For `standalone compression requires exactly one regular file`, supply one regular file or use a TAR variant for directories/multiple sources. For `thin AR archives reference external files and are unsupported`, obtain a self-contained regular AR archive instead.

## Symlink privileges and unsafe paths

Windows may report an OS privilege error when creating symlinks. Enable the necessary host capability or, if links are unnecessary, skip them:

```bash
snug extract backup.7z -C output/ --symlinks skip
```

`7z junction extraction is unsupported` cannot be fixed by granting symlink privileges. `7z creation cannot store a dangling symbolic link` means that py7zr cannot store that source link; use ZIP/TAR or skip it.

`error: unsafe archive:` indicates a rejected path or link, including traversal, absolute paths, escaping symlinks, or 7z output collisions. Use a clean destination when existing links cause containment failures. There is no switch to bypass the [security checks](security.md). A failed extraction may already have written some entries; inspect or discard its output before retrying.

## Password and encrypted-archive failures

For `7z archive requires a password; use --password or --password-file`, provide the prompt option or an existing one-line UTF-8 password file:

```bash
snug extract protected.7z -C output/ --password
snug extract protected.7z -C output/ --password-file password.txt
```

`cannot securely prompt here; use --password-file` occurs when Snug cannot hide prompt input. `password file must contain exactly one line` means the file contains embedded line breaks. `could not read 7z archive: incorrect password or corrupt archive` can mean either cause; verify the password and obtain an intact archive.

`encrypted RAR extraction is not supported by the installed libarchive backend` is a support limit, not a request for another password. ZIP encryption depends on the reader, and Snug cannot create encrypted ZIP. TAR and streams reject password protection. Use 7z for password-protected creation; see [Usage](usage.md#passwords).

## Repair failures and reinstallation

If Windows reports that `Get-FileHash` or another built-in cmdlet is not recognized, check how PowerShell was launched. A Python/cmd child of PowerShell 7 can pass incompatible module paths to Windows PowerShell 5.1. Current Snug scripts prioritize their own built-in modules; rerun the installer to replace older scripts. The `WindowsPowerShell\v1.0` directory name does not identify the running PowerShell version. Check `$PSVersionTable.PSVersion` in that interpreter instead.

Managed errors include `download checksum mismatch: ...`, `Python download checksum mismatch.`, `Homebrew installation failed. Check your connection, then run snug again.`, and `Dependency repair failed. Check your connection and run snug again.`

Check internet access and the actual underlying download/package-manager error, then run Snug again. Do not bypass checksum checks. A consistently failing URL or hash needs a reviewed [runtime update](runtime.md#runtime-update-procedure). Startup inventories may also report `Snug's libarchive binding needs repair` or `Snug's Python packages need repair`.

If another repair is running, wait for it to finish before retrying. Unix repair detects dead lock owners; Windows uses a mutex. After an interrupted installation, `An earlier installation backup exists; move it aside before reinstalling.` means a `.previous` directory remains. Inspect and move that backup to a separate location before reinstalling; preserve it if needed for recovery.

To reinstall application files, rerun the current installer. On macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/yklucz/snug/main/install.sh | bash
```

On Windows:

```powershell
irm https://raw.githubusercontent.com/yklucz/snug/main/install.ps1 | iex
```

For a custom Unix prefix, use the same `SNUG_PREFIX` as the original installation. Installers stage and validate the new copy before replacing the old one; they do not repair missing application files solely by launching Snug. For source installations, restore/reinstall the checkout and its chosen extras in the intended environment.

## Running offline

A managed installation whose startup checks pass does not download anything during ordinary launch. If checks fail offline, reconnect to repair first—even if the requested command uses only ZIP or TAR. Source installations do not run automatic repair and can use already installed backends offline.

Neither installers nor repair are a general offline bootstrap system. Successful local archive commands do not demonstrate that an unavailable download or a fresh platform install works.
