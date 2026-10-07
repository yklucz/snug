# Snug

A lightweight Python CLI archive manager for macOS and Linux, with explicit commands and an interactive terminal interface.

Windows development is maintained separately on the [`windows` branch](https://github.com/yklucz/snug/tree/windows).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/yklucz/snug/main/install.sh | bash
```

## Usage

```bash
snug
snug create backup.zip folder/
snug create backup.7z folder/
snug extract archive.rar -C output/
snug list backup.7z
snug info backup.7z
snug test backup.7z
snug doctor
snug formats
snug update --check
snug update
```

Run `snug` to open the terminal menu and file picker. Archive commands require their full names; paths are never interpreted as implicit commands.

Regular-file extraction stages each payload before atomic publication where supported. Optional [extraction limits](docs/usage.md#extraction-limits) bound selected entry counts and decoded sizes. Use `test` to verify payloads and `doctor` for offline installation diagnostics.

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
- [Updates](docs/updates.md)
- [Troubleshooting](docs/troubleshooting.md)

For contributions, see [CONTRIBUTING.md](CONTRIBUTING.md). For vulnerability reports, see [SECURITY.md](SECURITY.md).

## License

See [LICENSE](LICENSE).
