# Runtime management

[← Back to README](../README.md)

Managed installers deliver the application scripts, a platform launcher, [snug_runtime.py](../snug_runtime.py), and [runtime-lock.json](../runtime-lock.json). Source/pip installations use their own environment and do not run this management layer. See [Installation](installation.md) for user-facing setup.

## Lock file and pinned artifacts

The lock has schema version 1. It records HTTPS artifact URLs, SHA256 hashes, wheel filenames, package versions, and the exact DLL/license members to retain from Windows native packages. It is the authoritative source for download details; display the complete manifest from the repository root with:

```bash
python -m json.tool runtime-lock.json
```

The current principal artifacts are:

| Component | Locked value | Artifact URL |
|---|---|---|
| Shared Python binding | `libarchive-c` 5.3, `libarchive_c-5.3-py3-none-any.whl` | [Python package wheel](https://files.pythonhosted.org/packages/88/3f/ff00c588ebd7eae46a9d6223389f5ae28a3af4b6d975c0f2a6d86b1342b9/libarchive_c-5.3-py3-none-any.whl) |
| Windows fallback Python | CPython 3.14.8, embedded AMD64 ZIP | [Python runtime ZIP](https://www.python.org/ftp/python/3.14.8/python-3.14.8-embed-amd64.zip) |
| Windows native libarchive | MSYS2 UCRT64 3.8.9-6 | [Native package](https://mirror.msys2.org/mingw/ucrt64/mingw-w64-ucrt-x86_64-libarchive-3.8.9-6-any.pkg.tar.zst) |

Their SHA256 values are:

```text
libarchive-c wheel: 651550a6ec39266b78f81414140a1e04776c935e72dfc70f1d7c8e0a3672ffba
Windows Python ZIP: a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310
Windows libarchive: e19e2386eabd743494db13617aafc0a1dde159074580d2e1d6a919af49bdb6c8
```

These values describe the checked-in lock; they do not establish current remote availability or independent verification of the artifact publisher.

The Windows Python dependency set is pinned as follows. Each package has its wheel URLs and hashes in the lock:

| Package | Version |
|---|---|
| py7zr | 1.1.3 |
| brotli | 1.2.0 |
| inflate64 | 1.0.4 |
| multivolumefile | 0.2.3 |
| psutil | 7.2.2 |
| pybcj | 1.0.8 |
| pycryptodomex | 3.24.0 |
| pyppmd | 1.3.1 |
| texttable | 1.7.0 |
| backports.zstd | 1.0.0; used below Python 3.14 |

Wheel selection accepts universal Python 3 wheels, matching `cp310`–`cp314` x64 wheels, and compatible `abi3` wheels. Python 3.14 uses `compression.zstd`; earlier supported interpreters use `backports.zstd` to unpack native `.tar.zst` packages.

The lock does not pin Homebrew formulas or every interpreter that can be reused. Startup accepts py7zr 1.1.3 through 1.x and libarchive 3.8+ with required APIs. A compatible existing Windows environment may be reused; locked wheels and DLLs supply components when repair is required. Source pip extras use version ranges from [pyproject.toml](../pyproject.toml), not this artifact lock.

## Homebrew ownership

[runtime.sh](../runtime.sh) locates Homebrew through `SNUG_BREW`, `PATH`, or standard macOS/Linuxbrew paths. It resolves Python from the `py7zr` formula's `libexec/bin/` and libarchive from stable `opt/` links. This follows Homebrew upgrades without retaining a version-specific Cellar path.

Homebrew owns `py7zr`, Python, libarchive, and their shared dependencies. Snug owns its scripts and verified binding under `vendor/`. It does not uninstall or delete shared formulas during installation or repair. Healthy launches resolve the prefix and probe local dependencies with auto-update and analytics disabled.

Repair is serialized with an application-directory lock. It tries installation, binding repair, targeted formula reinstall, metadata refresh, and finally shared-dependency reinstall if earlier probes fail. That last step can affect formulas shared with other applications. Repair uses a temporary Homebrew cache for that operation and removes it afterward without deleting a pre-existing global cache.

## Windows managed runtime

[runtime.ps1](../runtime.ps1) accepts standard x64 CPython 3.10–3.14, excluding free-threaded builds and Windows Store aliases. It searches the application runtime, recorded interpreter, commands on `PATH`, and the `py` launcher. If necessary, it downloads and verifies the locked embedded Python into `python/`. ARM64 behavior is described in [Installation](installation.md#windows).

The embedded runtime is configured with a `python314._pth` file and `import site`. It does not include an installed pip toolchain. Wheels are unpacked into `packages/`; the binding lives in `vendor/`. Wheel tests, self-tests, bytecode caches, console scripts, and build headers are omitted. Package metadata and licenses remain.

Native packages contribute only the declared regular-file DLL and license members. The locked DLL members all begin with `ucrt64/bin/`; they are flattened into `native/`:

| Native package/version | DLL member basename |
|---|---|
| bzip2 1.0.8-4 | `libbz2-1.dll` |
| expat 2.8.5-1 | `libexpat-1.dll` |
| libarchive 3.8.9-6 | `libarchive-13.dll` |
| libb2 0.98.1-3 | `libb2-1.dll` |
| libiconv 1.19-1 | `libiconv-2.dll` |
| lz4 1.10.0-1 | `liblz4.dll` |
| xz 5.8.4-1 | `liblzma-5.dll` |
| zlib 1.3.2-2 | `zlib1.dll` |
| zstd 1.5.7-2 | `libzstd.dll` |

The LZ4 license is a separately locked download. `runtime.json` records interpreter/library selection; `.snug-files.json` records installed files and hashes in managed directories. Activation places owned packages/bindings on the import path and configures `LIBARCHIVE` from the launcher, recorded state, or bundled DLL. Library probes use child processes so Windows DLLs are not kept loaded while replacements occur. Repairs are serialized with a named mutex.

## Startup checks and repair

Managed launchers check both py7zr and libarchive before any CLI command. Checks include:

- Python compatibility; py7zr's supported version, streaming `WriterFactory`, and usable AES extension.
- The vendored binding's file inventory and per-file SHA256 hashes.
- Recorded file presence in owned `packages/` and `native/`, when those directories exist.
- Native libarchive 3.8+, required read formats (`7zip`, `ar`, `cab`, `cpio`, `iso9660`, `lha`, `rar`, `xar`, `warc`, `zip`), and `ar_bsd`/`cpio_newc` writers.

Native package and Python package inventories are checked for missing files plus functional backend probes; startup does not rehash every owned DLL or Python package file. It is a dependency health check, not complete ongoing tamper detection or exhaustive codec validation. In particular, it does not separately probe every RAR5/RPM/filter variant.

A failed check triggers repair. Windows repairs the binding and replaces failing/missing package or native directories from verified staged downloads, then rewrites runtime state and checks again. Homebrew repair follows the sequence above. Source/pip installations instead report missing-backend errors when those formats are requested.

Once checks pass, a normal managed launch makes no downloads and supports offline archive operations. Reinstalling application files, obtaining missing components, or repairing failed probes needs internet access. Even a native ZIP command through the managed launcher can require repair if an optional dependency is broken.

## Integrity and cleanup

Python's runtime downloader requires HTTPS and verifies each artifact against its locked SHA256. Windows PowerShell verifies the embedded Python ZIP with `Get-FileHash` before expansion. Managed wheel paths are validated, and native-package extraction selects only listed regular-file members. Damaged or incomplete downloads fail rather than replacing a working directory with their contents.

The top-level installers download application scripts and the lock over HTTPS from `main` (or the `SNUG_RAW_BASE` override). Those files do not have a separate pinned manifest or signature. Trust in the repository/transport and lock is therefore required; the artifact checks do not independently authenticate that lock. Homebrew manages its own formula downloads and integrity checks rather than using Snug's Windows hashes.

Installers validate a staged application before swapping it into place and keep a temporary `.previous` copy for rollback. Runtime helpers stage directories, retain backups during replacement, and remove normal download/cache/staging directories in cleanup handlers. Power loss or forceful termination can leave staging, lock, or backup directories; see [Troubleshooting](troubleshooting.md#repair-failures-and-reinstallation).

## Storage reporting

Managed repair reports application-owned storage and, on Homebrew, the newly added shared storage. It walks regular files, ignores symlinks into shared installations, and counts hardlinks once by device/inode. With `st_blocks`, it reports allocated bytes; otherwise it reports logical file bytes and explicitly notes that allocation may differ.

Homebrew accounting estimates the before/after `du -sk` difference across the prefix and excludes Snug's own directory if it is inside that prefix. It clamps a negative increase to zero and does not charge already installed shared dependencies again. This is an estimate for that repair, not a precise ownership ledger: concurrent Homebrew changes, filesystem compression/clones, directory overhead, and allocation semantics can affect the result.

From the repository root, this reports owned files for that checkout rather than another installed copy:

```bash
python snug_runtime.py --storage
```

## Runtime update procedure

1. Review upstream releases and obtain every artifact from its publisher. Compute SHA256 independently, then update versions, filenames, URLs, hashes, and native DLL/license member lists together in `runtime-lock.json`.
2. Ensure the Windows package set remains complete and wheel selection covers standard x64 CPython 3.10–3.14. Preserve `backports.zstd` coverage below 3.14 and the built-in Zstandard path on 3.14.
3. If changing the embedded Python minor version or supported ABI range, update the hard-coded `python314._pth` configuration and compatibility checks in the launchers/runtime code. Updating only the JSON version is insufficient.
4. Reconcile startup version/API checks and [pyproject.toml](../pyproject.toml) dependency ranges. Verify native reader/writer availability and actual fixtures; a successful import alone is insufficient.
5. Run the [development checks](development.md), runtime integrity/cleanup tests, and real clean-install, reuse, repair, and offline launch workflows on each affected platform. Include failed-download rollback and Windows DLL replacement. The [Windows test](../tests/test_windows_runtime.py) exercises bootstrap without existing Python, a password workflow, and deleted-DLL repair.
6. Update this page's version/DLL tables and related installation, format, and troubleshooting details. State which platforms and downloads were actually verified.
