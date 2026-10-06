# Snug

A lightweight Python CLI archive manager.

Works on macOS, Linux, and Windows. Source installations require Python 3.10+.

## Install

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/yklucz/snug/main/install.sh | bash
```

Homebrew (Linuxbrew on Linux) must be installed first; Snug explains how if it is
missing. The installer automatically installs `py7zr` and native `libarchive`,
reuses Homebrew's private py7zr Python environment, and keeps the small
`libarchive-c` binding beside Snug. It creates no Snug virtual environment.
Homebrew manages Python and py7zr; Snug keeps its binding in the application directory.

### Windows

```powershell
irm https://raw.githubusercontent.com/yklucz/snug/main/install.ps1 | iex
```

On 64-bit Windows 10/11, Snug reuses a compatible CPython 3.10–3.14 when available.
Otherwise it downloads the official minimal Python runtime automatically.
Production wheels and the required native libarchive DLLs are installed in
`%LOCALAPPDATA%\Snug`; no pip, compiler, MSYS2 installation, or developer tools are
retained. ARM64 Windows uses the x64 runtime through Windows' x64 emulation.

Both launchers check dependencies at startup and automatically repair missing or
incompatible components. A healthy installation works offline; failed repairs
show a clear error and can be retried by running `snug` with internet access.
Downloads are verified against pinned SHA256 hashes and temporary package caches
are removed. The storage report shows Snug's footprint and newly added shared
Homebrew storage, including dependencies such as Python. Existing shared dependencies add no new
storage. Filesystem allocation and temporary download space can exceed file sizes.
Savings depend on reuse: installing a new Homebrew Python adds its full package
and dependency footprint; it is included in Snug's storage report.

## Supported formats

| Format | Extract | Create |
|---|:---:|:---:|
| ZIP | ✅ | ✅ |
| TAR / GZIP / BZIP2 / XZ / LZMA | ✅ | ✅ |
| 7z | ✅ | ✅ |
| RAR / RAR5 | ✅ | ❌ |
| ZIPX | ✅ | ❌ |
| CAB / ISO | ✅ | ❌ |
| CPIO / AR | ✅ | ✅ |
| XAR / LHA / LZH | ✅ | ❌ |
| WARC / RPM / DEB | ✅ | ❌ |

The installers provide both `py7zr` and `libarchive` backends automatically.
Direct source/pip installations can use `pip install '.[all]'`; they also need a
native libarchive library. Those installations retain graceful optional-backend
errors and manage their own dependencies.

## Usage

```bash
snug

snug create backup.zip folder/
snug create backup.7z folder/

snug extract archive.rar -C output/
snug list backup.7z
snug info backup.7z
```

## Passwords

```bash
snug extract protected.7z --password
snug create protected.7z folder/ --password
```

Passwords are entered securely and are never printed.

## Features

- Interactive terminal interface
- Live progress, speed, and ETA
- Large-file streaming
- ZIP64 support
- Automatic archive format detection
- Secure extraction against path traversal and unsafe links
- Cross-platform support

## Development

```bash
python -m pip install '.[all,test]'
pytest -q
```

Managed-runtime dependency versions, artifact URLs, hashes, and Windows DLL
members are recorded in [runtime-lock.json](runtime-lock.json). Runtime updates
must verify the DLL dependency closure and run the installer tests on each
platform. Homebrew controls its own py7zr and native-library updates.

## License

See [LICENSE](LICENSE).
