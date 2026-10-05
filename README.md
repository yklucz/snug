# Snug

A lightweight Python CLI archive manager.

Works on macOS, Linux, and Windows with Python 3.10+.

## Install

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/yklucz/snug/main/install.sh | bash
```

### Windows

```powershell
irm https://raw.githubusercontent.com/yklucz/snug/main/install.ps1 | iex
```

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

Some formats require optional `py7zr` or `libarchive` backends.

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

## License

See [LICENSE](LICENSE).