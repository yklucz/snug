# Updates

[← Back to README](../README.md)

Snug checks releases and updates through explicit terminal commands. Automatic checking never installs a release. This guide covers macOS/Linux `main`; Windows installation and runtime maintenance remain on the [`windows` branch](https://github.com/yklucz/snug/tree/windows).

## Check or install explicitly

```bash
snug update --check
snug update
```

`--check` compares the running version with GitHub's latest stable release using semantic version precedence, including numeric components and prerelease rules. It does not install files. Same or older releases report that the current version is up to date; draft and prerelease responses are not offered as stable updates. A newer release reports the current/latest versions and the full `snug update` command.

An explicit metadata request has a five-second timeout. Offline, DNS, timeout, HTTP/rate-limit, invalid JSON, and unexpected metadata failures produce a concise error and exit status 2. No HTTP traceback is printed. Explicit checking works even when automatic checks are disabled.

## Automatic checks

Automatic checks are enabled by default and attempt release discovery no more than once every 24 hours. The timestamp is saved before requesting metadata, so failed attempts also count toward the interval. A background daemon performs the network request; archive operations do not wait for it or depend on its success. A short command may finish before discovery completes. Use `snug update --check` when an immediate answer is needed.

A completed check can print a brief notice to terminal stderr after a successful `create` or `extract`, or after leaving the terminal TUI successfully. Notices do not appear under `--quiet`, from `list`/`info`, in redirected output, or when checks are disabled. Notices are not repeated throughout the cached interval. No archive listing output is changed.

```bash
snug update --disable-checks
snug update --enable-checks
```

These commands only change the automatic-check preference; they do not install or contact GitHub. A failure to save an explicit preference is reported. For a single invocation or an offline test environment, set `SNUG_NO_UPDATE_CHECK=1`, for example:

```bash
SNUG_NO_UPDATE_CHECK=1 snug list backup.zip
```

## State and privacy

State is outside the repository:

| Platform | State file |
|---|---|
| macOS | `~/Library/Application Support/Snug/update.json` |
| Linux | `$XDG_STATE_HOME/snug/update.json`, or `~/.local/state/snug/update.json` when unset or relative |

The small JSON file records `last_check`, `latest_version` when known, and `automatic_checks` when explicitly configured. Writes use a temporary file followed by atomic replacement. Missing or corrupt state is treated as default state; future timestamps do not suppress checks indefinitely. An unavailable cache skips the automatic check and never fails an archive command. Explicit check results can still be reported when caching fails. A nonblocking operating-system file lock serializes state changes and is released when its process exits. The persistent `update.lock` file beside the JSON does not indicate an active owner.

Release checks request only the fixed [GitHub Releases API endpoint](https://api.github.com/repos/yklucz/snug/releases/latest) over HTTPS, with a static Snug updater user agent. They do not send archive names, filenames, local paths, usernames, hostnames, contents, format history, or usage analytics. There is no telemetry. A manual update additionally downloads the official release asset. HTTPS redirects are restricted to the supported GitHub download hosts.

## Which installations can update themselves?

`snug update` supports macOS/Linux copies created by the current [install.sh](../install.sh). The installer writes `.snug-install.json` inside the application directory and its launcher identifies that managed directory. Both records are required; a nearby `runtime.json` alone does not establish ownership.

Source/development checkouts and pip installations are managed externally and are not overwritten. Update a source checkout through its existing Git workflow; update a pip environment with the pip belonging to that environment. Older managed copies lacking the installer marker receive installation-method guidance; rerunning the installer records ownership. No separate update feature is promised for Windows by this `main` implementation.

Installations using a custom `SNUG_RAW_BASE` are recorded as externally managed, so a manual update cannot replace their chosen source with the canonical release. The installer and updater share an application lock; dependency repair refuses to run while that lock is held.

## Release requirements and secure replacement

A managed update requires a newer stable release tagged `vVERSION`, with exactly one uploaded source-distribution asset named `snug_archives-VERSION.tar.gz`. The metadata must provide GitHub's `sha256:` digest, the expected official download URL, and a valid bounded size. A tag without this asset cannot be installed by `snug update`. Future releases must include the updater, shared archive modules, runtime helper/launcher, and runtime lock in their source distribution.

The update process:

1. Download the HTTPS asset into a temporary sibling directory and verify its advertised size and SHA256 digest.
2. Validate archive member paths, types, duplicates, counts, and size limits. Retain only the explicit application-file allowlist; reject traversal, links, unsupported members, and incomplete release files.
3. Copy the existing application into staging, preserve its runtime state and owned dependencies, and replace application files with the verified release files.
4. Run the staged CLI version and runtime backend checks offline, without repairing dependencies.
5. Rename the installed application to a saved copy, replace it with staging, and repeat validation. Remove normal staging/download/backup files after success.

Downloads have a 30-second timeout, with limits of 16 MiB compressed and 64 MiB decompressed, including archive headers. Update staging needs writable space beside the application directory and enough room for its copied runtime. It does not reinstall or delete shared Homebrew formulas. Future runtime requirements incompatible with the preserved runtime can cause validation to refuse an application update; repair or reinstall through the documented installation workflow first.

## Failures, recovery, and offline use

Download, digest, archive, staging, and validation failures leave the installed application active. Replacement or final validation failures attempt to restore the saved installation. If restoration itself fails, Snug preserves the previous directory and reports its location; restore that directory to the original installation path before retrying. Do not delete the current installation to begin an update.

Directory replacement uses filesystem renames and rollback, including restoration after Ctrl+C. It is not a transaction across power loss or forced process termination, and it does not promise uninterrupted concurrent launches during the replacement. An interrupted update can leave a sibling application-update lock, staging directory, or saved copy. Inspect and preserve recovery copies; remove a stale application lock only after confirming that no update is running. Per-user state locks are released automatically by the operating system when a checker exits.

Automatic network failures remain silent and do not affect archive results. Explicit checks and installation need internet access, while normal operations use installed backends offline. The managed dependency launcher can still require repair when its own startup checks fail; see [Runtime management](runtime.md) and [Troubleshooting](troubleshooting.md).
