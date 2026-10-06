# Runtime management

[← Back to README](../README.md)

`main` maintains the macOS/Linux Homebrew runtime. The [Windows runtime guide](https://github.com/yklucz/snug/blob/windows/docs/runtime.md), PowerShell helpers, native dependency packaging, and bootstrap tests live on the secondary `windows` branch. Shared portable helpers and the complete artifact lock remain on `main`; removing every Windows conditional is not a goal.

Managed installers deliver the application scripts, a platform launcher, [snug_runtime.py](../snug_runtime.py), and [runtime-lock.json](../runtime-lock.json). Source/pip installations use their own environment and do not run this management layer. See [Installation](installation.md) for user-facing setup.

## Lock file and pinned artifacts

The lock has schema version 1. It records HTTPS artifact URLs, SHA256 hashes, wheel filenames, package versions, and the exact DLL/license members to retain from Windows native packages. It is the authoritative source for download details; display the complete manifest from the repository root with:

```bash
python -m json.tool runtime-lock.json
```

The shared `libarchive-c` binding is locked to version 5.3 and wheel `libarchive_c-5.3-py3-none-any.whl`, with SHA256 `651550a6ec39266b78f81414140a1e04776c935e72dfc70f1d7c8e0a3672ffba`. Windows Python, package, DLL, and license records are retained in the lock for shared reproducibility; they are maintained and exercised separately on `windows`.

These values describe the checked-in lock; they do not establish current remote availability or independent verification of the artifact publisher. The lock does not pin Homebrew formulas. Startup accepts py7zr 1.1.3 through 1.x and libarchive 3.8+ with required APIs. Source pip extras use the version ranges from [pyproject.toml](../pyproject.toml), rather than this managed artifact lock.

## Homebrew ownership

[runtime.sh](../runtime.sh) locates Homebrew through `SNUG_BREW`, `PATH`, or standard macOS/Linuxbrew paths. It resolves Python from the `py7zr` formula's `libexec/bin/` and libarchive from stable `opt/` links. This follows Homebrew upgrades without retaining a version-specific Cellar path.

Homebrew owns `py7zr`, Python, libarchive, and their shared dependencies. Snug owns its scripts and verified binding under `vendor/`. It does not uninstall or delete shared formulas during installation or repair. Healthy launches resolve the prefix and probe local dependencies with auto-update and analytics disabled.

Repair is serialized with an application-directory lock. It tries installation, binding repair, targeted formula reinstall, metadata refresh, and finally shared-dependency reinstall if earlier probes fail. That last step can affect formulas shared with other applications. Repair uses a temporary Homebrew cache for that operation and removes it afterward without deleting a pre-existing global cache.

## Startup checks and repair

Managed launchers check both py7zr and libarchive before archive commands or the terminal TUI. The Unix launcher can run `update` through its existing Python without probing or repairing optional backends when the runtime paths resolve. Checks include:

- Python compatibility; py7zr's supported version, streaming `WriterFactory`, and usable AES extension.
- The vendored binding's file inventory and per-file SHA256 hashes.
- Recorded file presence in owned `packages/` and `native/`, when those directories exist.
- Native libarchive 3.8+, required read formats (`7zip`, `ar`, `cab`, `cpio`, `iso9660`, `lha`, `rar`, `xar`, `warc`, `zip`), and `ar_bsd`/`cpio_newc` writers.

Native package and Python package inventories are checked for missing files plus functional backend probes; startup does not rehash every owned DLL or Python package file. It is a dependency health check, not complete ongoing tamper detection or exhaustive codec validation. In particular, it does not separately probe every RAR5/RPM/filter variant.

A failed check triggers Homebrew repair following the sequence above. Source/pip installations instead report missing-backend errors when those formats are requested.

Once dependency checks pass, the managed launcher makes no dependency downloads and supports offline archive operations. The CLI updater may attempt a best-effort periodic release-metadata request; it never installs automatically and does not block archive commands on network failure. See [Updates](updates.md). Reinstalling application files, obtaining missing components, or repairing failed probes needs internet access. Even a native ZIP command through the managed launcher can require repair if an optional dependency is broken.

## Integrity and cleanup

Runtime downloads and installer application downloads require HTTPS for the initial URL and every redirect. Python's runtime downloader rejects redirect downgrades before making the next request and verifies each artifact against its locked SHA256. The Unix installer restricts curl's initial and redirected protocols to HTTPS. Managed wheel paths are validated. Damaged or incomplete downloads fail rather than replacing a working directory with their contents.

The Unix installer downloads application scripts and the lock from `main` (or an HTTPS `SNUG_RAW_BASE` override). Those initial application downloads do not have a separate pinned manifest or signature; trust in the repository, HTTPS transport, and lock is required. Homebrew manages its own formula downloads and integrity checks. Explicit `snug update` uses a GitHub release asset and its SHA256 digest rather than downloading moving branch files; see [Updates](updates.md) for that separate application-update boundary.

Installers validate a staged application before swapping it into place and keep a temporary `.previous` copy for rollback. Runtime helpers stage directories, retain backups during replacement, and remove normal download/cache/staging directories in cleanup handlers. Power loss or forceful termination can leave staging, lock, or backup directories; see [Troubleshooting](troubleshooting.md#repair-failures-and-reinstallation).

## Storage reporting

Managed repair reports application-owned storage and, on Homebrew, the newly added shared storage. It walks regular files, ignores symlinks into shared installations, and counts hardlinks once by device/inode. With `st_blocks`, it reports allocated bytes; otherwise it reports logical file bytes and explicitly notes that allocation may differ.

Homebrew accounting estimates the before/after `du -sk` difference across the prefix and excludes Snug's own directory if it is inside that prefix. It clamps a negative increase to zero and does not charge already installed shared dependencies again. This is an estimate for that repair, not a precise ownership ledger: concurrent Homebrew changes, filesystem compression/clones, directory overhead, and allocation semantics can affect the result.

From the repository root, this reports owned files for that checkout rather than another installed copy:

```bash
python snug_runtime.py --storage
```

## Runtime update procedure

1. Review upstream releases and obtain artifacts from their publishers. Compute SHA256 independently, then update versions, filenames, URLs, hashes, and any native license/member lists together in `runtime-lock.json`.
2. Reconcile startup version/API checks and [pyproject.toml](../pyproject.toml) dependency ranges. Verify native reader/writer availability and actual fixtures; a successful import alone is insufficient.
3. Run the [development checks](development.md), runtime integrity/cleanup tests, and real clean-install, reuse, repair, and offline launch workflows on each affected platform. Include failed-download preservation of the installed runtime.
4. Port shared changes to `windows` intentionally and verify its compatible CPython wheel set, embedded runtime configuration, DLL replacement, and [Windows bootstrap tests](https://github.com/yklucz/snug/blob/windows/tests/test_windows_runtime.py) on Windows.
5. Update runtime, installation, format, and troubleshooting guidance. State which platforms and downloads were actually verified.

Runtime dependency repair differs from the user-facing `snug update` command: the latter replaces installer-owned application files from a validated release while preserving the selected runtime.
