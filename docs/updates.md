# Updates

[← Back to README](../README.md)

This `windows` branch shares Snug's release checks and automatic-check preferences with `main`. Windows application installation remains managed through the PowerShell installer. Automatic checking never installs a release.

## Check releases explicitly

```powershell
snug update --check
```

The command compares the running version with GitHub's latest stable release using semantic version precedence, including numeric components and prerelease rules. It does not install files. Same or older releases report that the current version is up to date; draft and prerelease responses are not offered as stable updates. A newer release reports both versions.

Explicit metadata requests have a five-second timeout. Offline, DNS, timeout, HTTP/rate-limit, invalid JSON, and unexpected metadata failures produce a concise error and exit status 2. No HTTP traceback is printed. Explicit checking still works when automatic checks are disabled.

## Automatic checks and preferences

Automatic checks are enabled by default and attempt discovery no more than once every 24 hours. A saved timestamp records the attempt before the request, so failed attempts also count toward the interval. A background daemon performs the request; archive operations do not wait for it or depend on its success. Short commands can finish before discovery completes; use `snug update --check` for an immediate answer.

A completed check can show a brief notice on terminal stderr after successful `create`/`extract` commands or after leaving the terminal TUI successfully. Notices do not appear under `--quiet`, from `list`/`info`, in redirected output, or when checks are disabled. Notices are not repeated throughout the cached interval. The notice's `snug update` command leads Windows users to external-installation guidance.

```powershell
snug update --disable-checks
snug update --enable-checks
```

These preference commands do not install or contact GitHub. An explicit failure to save the preference is reported. To suppress automatic checks in the current PowerShell session or an offline test environment:

```powershell
$env:SNUG_NO_UPDATE_CHECK = '1'
snug list backup.zip
Remove-Item Env:SNUG_NO_UPDATE_CHECK
```

## State and privacy

Windows state is `%LOCALAPPDATA%\Snug\state\update.json`. If `LOCALAPPDATA` is missing or relative, it falls back to `~/AppData/Local/Snug/state/update.json`. This shares the existing per-user Snug root and stays outside source checkouts.

The small JSON file records `last_check`, `latest_version` when known, and `automatic_checks` when explicitly configured. Writes use a temporary file and atomic replacement. Missing/corrupt state uses defaults; future timestamps cannot suppress checks indefinitely. An unavailable cache skips automatic checking and never fails an archive command. Explicit check results can still be reported when caching fails. A nonblocking operating-system lock serializes state changes and releases when its process exits; the persistent `update.lock` file does not indicate an active owner.

Release checks request only the fixed [GitHub Releases API endpoint](https://api.github.com/repos/yklucz/snug/releases/latest) over HTTPS, using a static updater user agent. They do not send archive names, filenames, local paths, usernames, hostnames, contents, format history, or usage analytics. There is no telemetry. HTTPS redirects are restricted to supported GitHub hosts.

## Install an updated Windows copy

```powershell
snug update
```

On Windows, this command reports that the installation is managed externally, exits with status 2, and leaves installed files unchanged. It refuses before requesting metadata or downloading an application. Secure automatic replacement through this command currently supports macOS/Linux managed copies on `main`; Windows self-update is deferred.

To update a managed Windows installation, rerun its installer:

```powershell
irm https://raw.githubusercontent.com/yklucz/snug/windows/install.ps1 | iex
snug --version
```

The installer downloads application scripts/lock over HTTPS into staging, prepares verified runtime dependencies, and validates the staged CLI before moving the existing installation. Application scripts and the lock rely on repository/transport trust; the scripts have no separate pinned signature or checksum manifest. Dependency artifacts retain the locked SHA256 checks described in [Runtime management](runtime.md). The replacement path attempts to restore the saved installation if activation or the final startup check fails.

For source/development checkouts, use the checkout's existing Git workflow and preserve local work. For pip installations, update using the pip belonging to that environment. A custom `SNUG_RAW_BASE` installation should retain its original trusted source. Snug does not overwrite these installations through `snug update`.

## Failures, recovery, and offline use

Automatic network failures remain silent and do not affect archive results. Explicit release checks and installer downloads need internet access; healthy managed installations and normal archive operations work offline. Missing/broken runtime components can still trigger the launcher's separate dependency repair.

Installer download/staging-validation failures retain the current installation. Replacement/startup failures attempt restoration of the saved copy. A power loss or forced termination can leave a `.previous` directory or staging artifacts; inspect and preserve recovery copies before retrying. Do not delete the current installation to begin an update. See [Installation](installation.md#windows), [Runtime management](runtime.md), and [Troubleshooting](troubleshooting.md) for repair and recovery.

Windows bootstrap verification is separate from ordinary offline pytest: `SNUG_WINDOWS_LIVE_BOOTSTRAP=1` explicitly enables its temporary embedded-runtime download and DLL-repair integration test. A macOS/Linux run of portable tests does not validate the PowerShell installer or Windows native runtime.
