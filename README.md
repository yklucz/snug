# Snug

A lightweight Python CLI archive manager.

Works on macOS, Linux, and Windows.

## Install

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/yklucz/snug/main/install.sh | bash
```

### Windows

```powershell
irm https://raw.githubusercontent.com/yklucz/snug/main/install.ps1 | iex
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

## Supported formats

Supports ZIP, TAR, GZIP, BZIP2, XZ, LZMA, 7z, RAR, ZIPX, CAB, ISO, CPIO, AR, XAR, LHA/LZH, WARC, RPM, and DEB.

See [Supported formats](docs/supported-formats.md) for creation support, backend requirements, and limitations.

## Documentation

- [Installation](docs/installation.md)
- [Usage](docs/usage.md)
- [Supported formats](docs/supported-formats.md)
- [Architecture](docs/architecture.md)
- [Security](docs/security.md)
- [Development](docs/development.md)
- [Runtime management](docs/runtime.md)
- [Troubleshooting](docs/troubleshooting.md)

## License

See [LICENSE](LICENSE).
