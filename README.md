# Snug

A lightweight Python CLI archive manager for Windows.

This secondary `windows` branch maintains the PowerShell installer, managed x64 runtime, and Windows dependency handling. The default [`main` branch](https://github.com/yklucz/snug/tree/main) is the primary project for macOS and Linux and the canonical source of shared CLI and archive-engine development.

## Install

```powershell
irm https://raw.githubusercontent.com/yklucz/snug/windows/install.ps1 | iex
```

The managed installer requires 64-bit Windows 10/11 and Windows PowerShell. See [Installation](docs/installation.md#windows) for runtime requirements and source installs. For macOS/Linux, use the installation instructions on `main`.

## Usage

```powershell
snug
snug create backup.zip folder/
snug create backup.7z folder/
snug extract archive.rar -C output/
snug list backup.7z
snug info backup.7z
```

With no arguments, `snug` opens the interactive terminal interface. Snug remains a CLI tool; all archive commands use explicit full command names.

## Supported formats

Creates ZIP, TAR variants, standalone compression streams, 7z, CPIO, and AR. Extracts these and additional formats including RAR, ZIPX, CAB, ISO, XAR, LHA/LZH, WARC, RPM, and DEB when the required backend is installed.

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

For contributions, see [CONTRIBUTING.md](CONTRIBUTING.md). For vulnerability reports, see [SECURITY.md](SECURITY.md).

## License

See [LICENSE](LICENSE).
