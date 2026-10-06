# Development

[← Back to README](../README.md)

## Branch policy

`main` remains the default branch and the primary macOS/Linux project. Shared CLI, archive formats, `ArchiveEngine`, extraction security, and backend APIs originate there. This secondary `windows` branch maintains the PowerShell installer/launcher, managed Windows runtime, native dependency handling, Windows tests, and Windows CI. Port relevant shared commits intentionally, typically by cherry-picking after review; do not fork the archive engine into two independent implementations.

Preserve full explicit commands and the terminal TUI. Snug has no desktop shell extensions, native graphical pickers, file associations, or GUI wrappers.

## Environment and checks

Use Python 3.10 or newer and an isolated environment from the repository root. Install native libarchive as described in [Installation](installation.md), then install Python extras and run the checks:

```bash
python -m pip install '.[all,test]'
python -m compileall .
npx --yes pyright@1.1.414
pytest -q
```

The Pyright command requires Node.js/npm and downloads that version when it is not cached. Its configuration in [pyproject.toml](../pyproject.toml) checks all four Python modules against Python 3.10 in standard mode. `compileall .` also traverses local environments; a smaller equivalent for the project files and available tests is:

```bash
python -m compileall -q snug.py snug_core.py snug_ext.py snug_runtime.py tests
```

The [CI workflow](../.github/workflows/tests.yml) on this branch targets Windows with standard x64 CPython 3.10 and 3.14, the managed runtime's supported range boundaries. It installs `.[all,test]`, prepares the locked Windows native libarchive dependencies, and runs compilation and pytest, including PowerShell HTTPS/bootstrap/repair tests. Wheel-selection tests cover every supported minor version from 3.10 through 3.14. Python 3.14 also runs pinned Pyright, built-artifact validation, and an isolated installed-wheel ZIP round trip. The no-existing-Python test validates the locked embedded fallback independently of the matrix interpreter. Unix-specific launcher tests skip on Windows. Review actual job results before claiming Windows compatibility; a test run on macOS/Linux does not validate Windows PowerShell or DLL behavior.

## Project layout

| Path | Responsibility |
|---|---|
| [snug.py](../snug.py) | Argument parser, command handlers, terminal menu/picker, presentation |
| [snug_core.py](../snug_core.py) | Archive engine, native containers/streams, detection, shared extraction policy, reports/progress |
| [snug_ext.py](../snug_ext.py) | Optional py7zr and libarchive adapters |
| [snug_runtime.py](../snug_runtime.py) | Managed dependency verification, activation, repair, and storage reporting |
| [install.sh](../install.sh), [install.ps1](../install.ps1) | Staged installation and launcher creation |
| [runtime.sh](../runtime.sh), [runtime.ps1](../runtime.ps1) | Platform startup and dependency repair |
| [runtime-lock.json](../runtime-lock.json) | Managed artifact URLs, versions, hashes, DLL/license members |
| [pyproject.toml](../pyproject.toml) | Package metadata, extras, CLI entry point, pytest/Pyright configuration |
| [MANIFEST.in](../MANIFEST.in) | Source-distribution contents and generated-file exclusions |
| `scripts/` | Developer release checks, including [artifact validation](../scripts/check_release.py) |
| `tests/` | Test suite and vendored archive fixtures |
| `docs/` | User and developer documentation |

This checkout declares version `1.8.0` in both package metadata and `snug_core.__version__`. Keep those values aligned with the CLI version and tests when preparing a release.

The flat module layout remains intentional for the current release: installers fetch those module filenames directly, and existing imports and the `snug = snug:main` entry point depend on them. `runtime.sh` and `runtime.ps1` are required managed-runtime launchers, so they remain public at the root with the installers.

## Public source and release artifacts

The Git repository contains all four implementation modules, tests and legal fixtures, documentation, CI, installers, runtime helpers, and lock metadata. Tests and security-related implementation remain public so the complete behavior can be audited. Public artifact URLs, versions, and SHA256 hashes in `runtime-lock.json` are reproducibility data.

The source distribution includes the modules, tests/fixtures, docs, developer scripts, lock, installers, runtime helpers, license, and contribution/security policies. It deliberately excludes `.github/`, local settings, generated runtime directories, caches, environments, and test outputs. CI workflows remain in the Git repository.

The wheel installs `snug`, `snug_core`, and `snug_ext`, the CLI entry point, and package metadata/license. It does not install tests, docs, CI, managed dependencies, or the managed-only `snug_runtime.py`, lock, and platform scripts into `site-packages`. The complete runtime-management source remains public in Git and the source distribution. Source/pip commands use their chosen environment rather than automatic repair.

Build both artifacts in a temporary or ignored output directory:

```bash
python -m pip install build
python -m build
python scripts/check_release.py dist
```

The release checker requires one source distribution and one wheel in a clean output directory. It compares included public source files with the checkout and rejects unnecessary wheel contents or local/generated files. Install the wheel into a fresh environment and run `snug --version` plus a native ZIP round trip. Validate the source distribution from outside the checkout so imports cannot silently fall back to repository files. Managed Windows artifacts are pinned in the lock; Homebrew packages, pip extras, and the setuptools/build toolchain use version ranges, so identical dependency resolution and byte-for-byte reproducible artifacts are not promised.

## Layout work deferred to 2.0

A future `src/snug/` package may separate the CLI, core policy, runtime management, and backends. Treat that as a 2.0 migration with compatibility coverage for imports, the CLI entry point, pip builds, installer download paths, and platform launchers. The current cleanup preserves the flat layout and root install URLs.

## Tests and fixture provenance

The suite groups native formats, CLI behavior, extraction security, optional backends, terminal UI, managed runtime helpers, and Windows bootstrap behavior. Optional tests use import/availability skips when py7zr or native libarchive cannot load. Symlink tests may skip when privileges are missing; Windows bootstrap tests require a Windows host. Review skips before making backend or cross-platform claims.

Fixture provenance is documented in [tests/fixtures/README.md](../tests/fixtures/README.md), with byte sizes, SHA256 hashes, and upstream URLs in [manifest.json](../tests/fixtures/manifest.json). Independent `test_read_format_*` archives come from the official libarchive v3.8.1 tests, decoded from uuencoded files; the ISO fixture was also decompressed from Unix `.Z`. Original licensing and notices are retained beside the fixtures. The XAR and traditional encrypted ZIP samples were generated independently with libarchive-c and have locally generated MIT-licensed content. Tests also generate native, adversarial, CPIO/7z, and AR/DEB archives at runtime.

For new fixtures, record exact provenance and hashes, retain required licenses, and distinguish upstream evidence from locally generated samples. Do not fabricate a source URL for a local sample or use a Snug-created round trip as the only independent-reader evidence. [.gitattributes](../.gitattributes) marks archive fixtures as binary to prevent newline conversion; add a matching rule for any new fixture extension. Fixtures should remain small and tests should avoid network access unless specifically testing an installer download path.

## Coding expectations

Preserve Python 3.10 compatibility, annotations, the separation between UI and archive logic, lazy optional imports, and bounded payload I/O. Route extraction through shared safety helpers or checked writers; do not add unrestricted backend extraction. Escape untrusted output and redact passwords in backend errors. Keep failure messages actionable and make skipped entries explicit.

Creation must preserve existing archives on backend failure, clean staging files where possible, and retain the engine's temporary-file replacement lifecycle. Keep metadata claims limited to fields actually established by the backend. Use the [Architecture](architecture.md) and [Security](security.md) pages as design context.

## Add a backend or supported format

1. Implement the `ArchiveBackend` contract: name, read/write format sets, availability, `can_read`, `can_write`, listing, metadata, creation, and checked extraction. An optional nonrecursive `detect` hook can assist content detection.
2. Register the backend in `_backends()` in the intended preference order. Keep imports lazy and ensure unavailable optional dependencies do not break native source usage.
3. For a new format, update `ArchiveFormat`, suffix mappings, appropriate signature/backend detection, capability sets, and helpful errors. Creation format choices come from the enum, so read-only formats must still reject writes correctly.
4. Add round trips for writable formats and independent fixtures for readers. Cover wrong/missing dependencies, renamed inputs, unsupported codecs/encryption, progress, metadata, links, malformed paths, overwrite policies, and failed creation without partial final output. Check creation rejection for read-only formats.
5. Update optional dependencies and runtime checks only when necessary. If managed artifacts change, follow the [runtime update procedure](runtime.md#runtime-update-procedure), including wheel ABI coverage and native DLL/license manifests.
6. Update the [format matrix](supported-formats.md), [Usage](usage.md), and relevant installation/troubleshooting guidance. Run checks with the optional backend present and absent, and validate actual platform workflows before expanding platform claims.

If format support changes, update the tests and provenance together. A new libarchive release advertising a reader is insufficient by itself to claim new Snug support.
