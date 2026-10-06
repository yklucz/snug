# Contributing

`main` is the default branch, the primary macOS/Linux project, and the canonical development branch for shared CLI/archive functionality. `windows` maintains the Windows installer, PowerShell launcher, managed runtime, native dependencies, tests, and CI. Develop shared archive fixes on `main`, then intentionally port or cherry-pick the relevant commits to `windows`; keep one shared `ArchiveEngine` implementation.

Use Python 3.10 or newer and an isolated environment. Install native libarchive using the [installation instructions](docs/installation.md), then run:

```powershell
git clone --branch windows https://github.com/yklucz/snug.git
cd snug
python -m pip install '.[all,test]'
python -m compileall -q snug.py snug_core.py snug_ext.py snug_runtime.py snug_update.py tests
npx --yes pyright@1.1.414
pytest -q
```

Pyright requires Node.js/npm. Optional-backend and platform tests can skip when dependencies or host capabilities are unavailable; note those skips in your pull request.

- Add meaningful tests for behavioral changes and regression tests for security fixes.
- Preserve Python 3.10 compatibility, public CLI behavior, lazy optional imports, and the root `windows/install.ps1` raw installer URL.
- Keep tests and implementation public; add dependencies only when necessary.
- Keep fixtures small and redistributable, with provenance and required licenses in [tests/fixtures](tests/fixtures/README.md).
- Keep downloaded runtimes, caches, local settings, generated outputs, and credentials out of commits.
- Route archive writes through the existing safety helpers and retain download verification.

Describe the problem, resulting behavior, and checks run in your pull request. See [Development](docs/development.md) for architecture, artifact policy, and runtime changes. Report unpatched vulnerabilities through the [Security Policy](SECURITY.md).
