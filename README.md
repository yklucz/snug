# Snug

A lightweight Python CLI archive manager.

## Install

Requires **Python 3.10+**.

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/yklucz/snug/main/install.sh | bash
```

### Windows

Run in PowerShell:

```powershell
irm https://raw.githubusercontent.com/yklucz/snug/main/install.ps1 | iex
```

## Usage

```bash
snug
```

Create an archive:

```bash
snug create backup.tar.gz folder/
```

Extract an archive:

```bash
snug extract backup.tar.gz
```

List archive contents:

```bash
snug list backup.tar.gz
```

Show archive information:

```bash
snug info backup.tar.gz
```

Supports ZIP, TAR, GZIP, BZIP2, and XZ.