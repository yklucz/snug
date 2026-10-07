# Troubleshooting

[← Back to README](../README.md)

This guide covers macOS/Linux `main`. Windows setup and repair are documented on the [`windows` branch](https://github.com/yklucz/snug/blob/windows/docs/troubleshooting.md).

First distinguish a managed launcher from a source/pip command. Managed errors begin with `Snug dependency error:` and concern startup checks or repair. Source CLI errors generally begin with `error:`. See [Installation](installation.md) for setup and [Runtime management](runtime.md) for repair behavior.

Start with `snug doctor` and `snug formats` when Python can launch Snug. `doctor --json` and `formats --json` provide schema-versioned reports. These commands are offline and read-only: they do not check for updates, repair dependencies, or download missing components. An unavailable optional backend can be healthy in a source/pip environment; broken required managed components make doctor exit with status 2. A missing managed interpreter still requires installation/repair outside diagnostics.

## `snug: command not found`

On macOS/Linux, the default launcher is `~/.local/bin/snug`. Check it directly, then add its directory to your shell profile:

```bash
"$HOME/.local/bin/snug" --version
export PATH="$HOME/.local/bin:$PATH"
```

Use the actual prefix if you set `SNUG_PREFIX`. Open a new terminal after editing the profile.

For pip installations, activate the correct environment or run `.venv/bin/snug` directly. `python snug.py --version` also works from a source checkout.

## Unsupported Python

The package requires Python 3.10+. Check the interpreter associated with the source environment:

```bash
python --version
python -m pip --version
```

Create a new environment with a supported Python if necessary; changing a system `python` command does not change an existing virtual environment. Managed checks can report `Python 3.10+ is required`.

## Homebrew missing

Actual launcher messages include `Please install Homebrew (Linuxbrew on Linux) from https://brew.sh, then run the installer or snug again.` and `Homebrew executable not found.`

Install Homebrew, restore its executable to `PATH`, or point to an existing installation:

```bash
export SNUG_BREW="/opt/homebrew/bin/brew"
snug --version
```

Use your actual path (`/usr/local/bin/brew` or `/home/linuxbrew/.linuxbrew/bin/brew` are other searched locations). The Unix managed installer requires Homebrew even when system Python/libarchive exists. Choose a source installation to use distribution packages instead.

## libarchive cannot load

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

Restart the Python process after changing library discovery settings. For managed installations, reconnect and run Snug again so the launcher can repair; if application scripts or the lock are missing, rerun the installer.

## Missing py7zr or unavailable backend

Actual errors include `7z support requires py7zr (install Snug with the 7z extra)` and `7z creation requires the py7zr backend (install snug-archives[7z])`.

From a source checkout, install the extra in the same environment:

```bash
python -m pip install '.[7z]'
snug info backup.7z
```

libarchive provides fallback 7z extraction when py7zr is unavailable, but 7z creation requires py7zr. For managed startup errors such as `py7zr 1.1.3 through 1.x is required` or `py7zr is missing its streaming writer API`, reconnect and rerun the managed command to repair. Installing into an unrelated system Python does not fix Homebrew's selected interpreter.

## Unsupported format or codec

Errors can include `cannot determine archive format`, `the installed backend cannot read this FORMAT archive or its compression method`, `FORMAT creation is not supported; choose a writable format`, or a more specific decoder error.

Check `snug formats` and the [format matrix and limits](supported-formats.md). Missing backends require installation; unsupported codecs require a suitable native build or conversion with a tool that supports the original archive. Use `--format` only for creation with an unknown output suffix; extraction has no format override. Renaming an unsupported archive does not add a decoder. A header that is detected successfully may still be truncated or corrupt; `snug test ARCHIVE` consumes payloads to check decoder-supported integrity.

For `standalone compression requires exactly one regular file`, supply one regular file or use a TAR variant for directories/multiple sources. For `thin AR archives reference external files and are unsupported`, obtain a self-contained regular AR archive instead.

## Links and unsafe paths

If links are unnecessary, skip them:

```bash
snug extract backup.7z -C output/ --symlinks skip
```

`7z junction extraction is unsupported` cannot be fixed by granting symlink privileges. `7z creation cannot store a dangling symbolic link` means that py7zr cannot store that source link; use ZIP/TAR or skip it.

`error: unsafe archive:` indicates a rejected path or link, including traversal, absolute paths, escaping symlinks, or duplicate output paths. Use a clean destination when existing links cause containment failures. There is no switch to bypass the [security checks](security.md). Regular-file payloads stay staged until decoding succeeds, but a failed extraction can still leave directories, links, or files committed before a later publication failure. Inspect its output before retrying.

## Limits and destination publication failures

An explicit extraction limit violation exits with status 4. Check the selected entry count and declared/decoded byte limits before increasing a bound. `--max-files` counts selected archive entries, including directories and links; declared-size preflight can count entries later skipped. Use `--member` to narrow selection. Ratio metadata is unavailable for some formats, so combine ratio limits with total/per-member byte limits. See [Usage](usage.md#extraction-limits) for exact units; `1M` is decimal and `1MiB` is binary.

`cannot atomically replace directory with file` means a regular-file member conflicts with a destination directory. Choose another destination or use `--no-overwrite`. `exclusive atomic file publication is unavailable` means the filesystem cannot perform the hard-link publication used by `--no-overwrite`; Snug fails safely instead of copying incomplete data to the final path. Use a destination on a filesystem that supports the operation.

A locked destination, especially on Windows, can prevent replacement after the new payload is validated. Close the application holding it or choose another destination; the old regular-file target is retained if replacement fails. Handled failures attempt every staging cleanup. Process kills, power loss, or filesystem cleanup errors can leave `.snug-part-*` files; inspect them before removal. These guarantees apply per regular file, not as rollback of the entire extraction.

## Password and encrypted-archive failures

For `7z archive requires a password; use --password or --password-file`, provide the prompt option or an existing one-line UTF-8 password file:

```bash
snug extract protected.7z -C output/ --password
snug extract protected.7z -C output/ --password-file password.txt
```

`cannot securely prompt here; use --password-file` occurs when Snug cannot hide prompt input. `password file must contain exactly one line` means the file contains embedded line breaks. `could not read 7z archive: incorrect password or corrupt archive` can mean either cause; verify the password and obtain an intact archive.

`encrypted RAR extraction is not supported by the installed libarchive backend` is a support limit, not a request for another password. ZIP encryption depends on the reader, and Snug cannot create encrypted ZIP. TAR and streams reject password protection. Use 7z for password-protected creation; see [Usage](usage.md#passwords).

## Repair failures and reinstallation

Managed errors include `download checksum mismatch: ...`, `Homebrew installation failed. Check your connection, then run snug again.`, and `Dependency repair failed. Check your connection and run snug again.`

Check internet access and the actual underlying download/package-manager error, then run Snug again. Do not bypass checksum checks. A consistently failing URL or hash needs a reviewed [runtime update](runtime.md#runtime-update-procedure). Startup inventories may also report `Snug's libarchive binding needs repair` or `Snug's Python packages need repair`.

If another repair is running, wait for it to finish before retrying. Unix repair detects dead lock owners. After an interrupted installation, `An earlier installation backup exists; move it aside before reinstalling.` means a `.previous` directory remains. Inspect and move that backup to a separate location before reinstalling; preserve it if needed for recovery.

To reinstall application files, rerun the current installer. On macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/yklucz/snug/main/install.sh | bash
```

For a custom Unix prefix, use the same `SNUG_PREFIX` as the original installation. Installers stage and validate the new copy before replacing the old one; they do not repair missing application files solely by launching Snug. For source installations, restore/reinstall the checkout and its chosen extras in the intended environment.

## Running offline

A managed installation whose dependency startup checks pass does not download dependency artifacts during ordinary launch. If checks fail offline, reconnect to repair first—even if the requested command uses only ZIP or TAR. Source installations do not run automatic repair and can use already installed backends offline.

`doctor` and `formats` bypass managed repair when an existing Python is available, so they can report broken optional components offline. Unlike archive commands, they also bypass periodic update checks and do not write updater state.

Neither installers nor repair are a general offline bootstrap system. Successful local archive commands do not demonstrate that an unavailable download or a fresh platform install works.

## Update checks and failed updates

An explicit `snug update --check` failure reports a concise error for offline, DNS, timeout, HTTP/rate-limit, or invalid metadata responses. Reconnect and retry; normal archive commands still work with their installed backends. Disable periodic checks with `snug update --disable-checks`, or set `SNUG_NO_UPDATE_CHECK=1` for a single command or offline test environment.

If `snug update` reports that the installation is managed externally, use the method that installed it. Pip environments should be updated with that environment's pip; source checkouts should be updated through their normal Git workflow. Older managed copies can be reinstalled with `install.sh` to record installation ownership. Snug does not overwrite checkouts or infer ownership merely from a nearby runtime file.

A newer tag alone is insufficient for a managed update: the release must publish the expected source asset with a SHA256 digest. Download, digest, staging, or validation failures retain the active application. Replacement failures attempt to restore its saved copy. Resolve the reported cause before retrying; do not bypass verification or delete the current application. After a crash or forced termination, preserve any staged or backup copy until its contents have been inspected. See [Updates](updates.md) for the full release and recovery boundary.
