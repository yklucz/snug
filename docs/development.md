# Development

[← Back to README](../README.md)

## Platform branches and product boundary

`main` is the default and canonical shared-code branch, focused on macOS and Linux. [`windows`](https://github.com/yklucz/snug/tree/windows) preserves and maintains the Windows installer, PowerShell launcher, managed x64 dependencies, packaging, and Windows-specific tests/CI. Shared archive fixes originate on `main`; port focused commits intentionally, preferably with cherry-picks once the branches diverge. Avoid developing separate ArchiveEngine implementations.

Snug remains a CLI and terminal TUI. Keep `create`, `extract`, `list`, `info`, `test`, `doctor`, `formats`, and `update` explicit, retain the no-argument terminal menu, and pass paths as argument values. Terminal-inserted paths need quoting appropriate to the caller's shell; they do not require desktop integration. Do not introduce graphical pickers, desktop launchers, file associations, implicit path commands, or command aliases.

## Environment and checks

Use Python 3.10 or newer and an isolated environment from the repository root. Install native libarchive as described in [Installation](installation.md), then install Python extras and run the checks:

```bash
python -m pip install '.[all,test]'
python -m compileall .
npx --yes pyright@1.1.414
pytest -q
```

The Pyright command requires Node.js/npm and downloads that version when it is not cached. Its configuration in [pyproject.toml](../pyproject.toml) checks the CLI, archive, runtime, and updater modules against Python 3.10 in standard mode. `compileall .` also traverses local environments; a smaller equivalent for the project files and available tests is:

```bash
python -m compileall -q snug.py snug_core.py snug_ext.py snug_runtime.py snug_update.py tests
```

The [CI workflow](../.github/workflows/tests.yml) runs core tests on Linux with Python 3.10–3.14 and platform tests on macOS with Python 3.14. It installs `.[all,test]` plus native libarchive and runs compilation and pytest. Linux Python 3.14 also runs the pinned Pyright version and validates built artifacts. Windows CI runs separately on `windows`. Review actual job results before claiming a platform or Python version has passed; a local macOS run does not validate Linux or Windows.

## Project layout

| Path | Responsibility |
|---|---|
| [snug.py](../snug.py) | Argument parser, command handlers, terminal menu/picker, presentation |
| [snug_core.py](../snug_core.py) | Archive engine, native containers/streams, detection, shared extraction policy, reports/progress |
| [snug_ext.py](../snug_ext.py) | Optional py7zr and libarchive adapters |
| [snug_runtime.py](../snug_runtime.py) | Managed dependency verification, activation, repair, and storage reporting |
| [snug_update.py](../snug_update.py) | Release metadata, periodic check state, installer ownership, staged manual updates |
| [install.sh](../install.sh) | Staged macOS/Linux installation and launcher creation |
| [runtime.sh](../runtime.sh) | Homebrew runtime startup and dependency repair |
| [runtime-lock.json](../runtime-lock.json) | Managed artifact URLs, versions, hashes, DLL/license members |
| [pyproject.toml](../pyproject.toml) | Package metadata, extras, CLI entry point, pytest/Pyright configuration |
| [MANIFEST.in](../MANIFEST.in) | Source-distribution contents and generated-file exclusions |
| `scripts/` | Developer release checks, including [artifact validation](../scripts/check_release.py) |
| `tests/` | Test suite and vendored archive fixtures |
| `docs/` | User and developer documentation |

This checkout declares version `1.9.0` in both package metadata and `snug_core.__version__`. Keep those values aligned with the CLI version and tests when preparing a release.

The flat module layout remains intentional for the current release: installers fetch those module filenames directly, and existing imports and the `snug = snug:main` entry point depend on them. `install.sh` and `runtime.sh` remain public at the root. Windows PowerShell scripts remain at the root of `windows`, with its raw installer URL pointing to that branch.

## Public source and release artifacts

The Git repository contains the five implementation modules, tests and legal fixtures, documentation, CI, installers, runtime helpers, and lock metadata. Tests and security-related implementation remain public so the complete behavior can be audited. Public artifact URLs, versions, and SHA256 hashes in `runtime-lock.json` are reproducibility data.

The source distribution includes the modules, tests/fixtures, docs, developer scripts, lock, installers, runtime helpers, license, and contribution/security policies. It deliberately excludes `.github/`, local settings, generated runtime directories, caches, environments, and test outputs. CI workflows remain in the Git repository.

The wheel installs `snug`, `snug_core`, `snug_ext`, and `snug_update`, the CLI entry point, and package metadata/license. It does not install tests, docs, CI, managed dependencies, or the managed-only `snug_runtime.py`, lock, and platform scripts into `site-packages`. The complete runtime-management source remains public in Git and the source distribution. Source/pip commands use their chosen environment rather than automatic repair.

After a checkout adds an installed module or changes package metadata, refresh an existing editable installation with `python -m pip install --no-deps -e .`. Editable installs track changes to mapped source files, but setuptools does not automatically regenerate their module mappings or installed version metadata.

Build both artifacts in a temporary or ignored output directory:

```bash
python -m pip install build
python -m build
python scripts/check_release.py dist
```

The release checker requires one source distribution and one wheel in a clean output directory. It compares included public source files with the checkout and rejects unnecessary wheel contents or local/generated files. Install the wheel into a fresh environment and run `snug --version` plus a native ZIP round trip. Validate the source distribution from outside the checkout so imports cannot silently fall back to repository files. Managed Windows artifacts are pinned in the lock; Homebrew packages, pip extras, and the setuptools/build toolchain use version ranges, so identical dependency resolution and byte-for-byte reproducible artifacts are not promised.

For both wheel and editable installations, run the environment's Python with `-I` against [scripts/check_installed_cli.py](../scripts/check_installed_cli.py). This checks the installed executable, module imports, and updater dispatch with mocked release responses and temporary preferences. CI runs it in separate fresh environments so checkout imports cannot hide missing installed modules.

## Layout work deferred to 2.0

A future `src/snug/` package may separate the CLI, core policy, runtime management, and backends. Treat that as a 2.0 migration with compatibility coverage for imports, the CLI entry point, pip builds, installer download paths, and platform launchers. The current cleanup preserves the flat layout and root install URLs.

## Tests and fixture provenance

The suite groups native formats, CLI behavior, extraction transactions/limits/security, unified inspection, payload integrity, offline diagnostics/capabilities, optional backends, terminal UI/path handling, managed runtime helpers, and the updater. Failure tests cover corrupted/truncated payloads, wrong passwords, write/close/cleanup faults, interrupts, destination preservation, and no-overwrite races. Limit tests include unavailable/lying metadata and actual-byte overflow. Optional tests use import/availability skips when py7zr or native libarchive cannot load. Symlink tests may skip when privileges are missing. Windows bootstrap tests are preserved on `windows` and require a Windows host. Review skips before making backend or cross-platform claims.

Fixture provenance is documented in [tests/fixtures/README.md](../tests/fixtures/README.md), with byte sizes, SHA256 hashes, and upstream URLs in [manifest.json](../tests/fixtures/manifest.json). Independent `test_read_format_*` archives come from the official libarchive v3.8.1 tests, decoded from uuencoded files; the ISO fixture was also decompressed from Unix `.Z`. Original licensing and notices are retained beside the fixtures. The XAR and traditional encrypted ZIP samples were generated independently with libarchive-c and have locally generated MIT-licensed content. Tests also generate native, adversarial, CPIO/7z, and AR/DEB archives at runtime.

For new fixtures, record exact provenance and hashes, retain required licenses, and distinguish upstream evidence from locally generated samples. Do not fabricate a source URL for a local sample or use a Snug-created round trip as the only independent-reader evidence. [.gitattributes](../.gitattributes) marks archive fixtures as binary to prevent newline conversion; add a matching rule for any new fixture extension. Fixtures should remain small. Updater and archive tests must run offline; mock release metadata and downloads, isolate update state, and never update a developer installation. Explicit live installer verification is separate from the normal test suite.

## Coding expectations

Preserve Python 3.10 compatibility, annotations, the separation between UI and archive logic, lazy optional imports, and bounded payload I/O. Route regular-file extraction through `SafeOutputFile` and the shared byte budget; do not add unrestricted backend extraction or remove an existing regular target before commit. Preserve primary failures while attempting all staging cleanup. CLI and TUI must pass the same engine settings. Escape untrusted output and redact passwords in backend errors. Keep failure messages actionable and make skipped entries explicit.

Update checks must not fail archive operations. Keep update state outside the checkout, write it atomically, and retain the installed application on download, checksum, staging, validation, and replacement failures. Test those failures with disposable installations.

Creation must preserve existing archives on backend failure, clean staging files where possible, and retain the engine's temporary-file replacement lifecycle. Keep metadata claims limited to fields actually established by the backend. Use the [Architecture](architecture.md) and [Security](security.md) pages as design context.

`test` must consume payloads rather than merely list headers, with no extracted files or whole-member buffering. `doctor`/`formats` must remain offline and read-only; test that they bypass update checks and repairs. `update` must not instantiate `ArchiveEngine`. Keep capability reports based on backend declarations and actual native registration results, with codec/encryption limits stated separately.

## Add a backend or supported format

1. Implement the `ArchiveBackend` contract: name, read/write format sets, availability, `can_read`, `can_write`, unified `inspect` returning `ArchiveInspection`, bounded payload `test`, creation, and checked extraction. Keep `list_entries` and `metadata` compatible through inspection. An optional nonrecursive `detect` hook can assist content detection.
2. Register the backend in `_backends()` in the intended preference order. Keep imports lazy and ensure unavailable optional dependencies do not break native source usage.
3. For a new format, update `ArchiveFormat`, suffix mappings, appropriate signature/backend detection, capability sets, and helpful errors. Creation format choices come from the enum, so read-only formats must still reject writes correctly.
4. Add round trips for writable formats and independent fixtures for readers. Cover wrong/missing dependencies, renamed inputs, unsupported codecs/encryption, progress, metadata, links, malformed paths, duplicate outputs, overwrite races, extraction limits, payload-test failures, and staging cleanup without partial final output. Check creation rejection for read-only formats.
5. Update optional dependencies and runtime checks only when necessary. If managed artifacts change, follow the [runtime update procedure](runtime.md#runtime-update-procedure), including wheel ABI coverage and native DLL/license manifests.
6. Update the [format matrix](supported-formats.md), [Usage](usage.md), and relevant installation/troubleshooting guidance. Run checks with the optional backend present and absent, and validate actual platform workflows before expanding platform claims.

If format support changes, update the tests and provenance together. A new libarchive release advertising a reader is insufficient by itself to claim new Snug support.

## Reliability scope and deferred work

The reliability implementation adds shared regular-file transactions, explicit extraction limits, unified inspection, `test`, `doctor`, `formats`, and terminal create/extract/test options. It preserves the updater and platform branch split. Shell completion, batch extraction redesign, archive editing, new formats, cloud/URL inputs, and desktop integration remain outside this phase. Change the version only after shared implementation, docs, main verification, and the Windows port are complete.
