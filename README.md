# Snug

A lightweight Python CLI archive manager.

Works on macOS, Linux, and Windows. Source installations require Python 3.10+.

## Install

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/yklucz/snug/main/install.sh | bash
```

Requires Homebrew or Linuxbrew. The installer sets up `py7zr`, `libarchive`, and required Python bindings automatically.

### Windows

```powershell
irm https://raw.githubusercontent.com/yklucz/snug/main/install.ps1 | iex
```

On 64-bit Windows 10/11, the installer uses a compatible Python installation when available or downloads a minimal runtime automatically.

Both installers verify downloaded components and repair missing dependencies when Snug starts.

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

Extended formats use `py7zr` and `libarchive`.

For source installations:

```bash
pip install '.[all]'
```

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
- Automatic format detection
- Secure archive extraction
- Cross-platform support

## Development

```bash
python -m pip install '.[all,test]'
pytest -q
```

Managed runtime versions and hashes are stored in [`runtime-lock.json`](runtime-lock.json).

## License

See [LICENSE](LICENSE).