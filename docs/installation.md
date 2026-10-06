# Installation

[← Back to README](../README.md)

This `windows` branch maintains Windows installation and runtime support. The default `main` branch is the primary macOS/Linux project and the source of shared CLI/archive development. Choose a managed installer for automatic backend setup and dependency repair, or install from source to manage Python and dependencies yourself. Managed installation and dependency downloads require internet access. A healthy installation can work offline.

## Requirements

- Source installations require Python 3.10 or newer, as declared in [pyproject.toml](../pyproject.toml). The base package has no third-party Python dependencies.
- The macOS/Linux installer requires Bash, `curl`, and Homebrew (Linuxbrew on Linux). It uses Homebrew's Python environment for `py7zr`.
- The Windows managed installer targets 64-bit Windows 10/11 and requires Windows PowerShell (`powershell.exe`). It accepts standard, GIL-enabled x64 CPython 3.10–3.14, or downloads the locked embedded x64 runtime.
- Extended formats require both the `libarchive-c` Python binding and a native libarchive library. Managed installations require libarchive 3.8+ with the readers and writers checked at startup. Source installations depend on their installed library's capabilities.

## macOS

Install Homebrew using its instructions at `https://brew.sh`, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/yklucz/snug/main/install.sh | bash
```

The installer installs or repairs the `py7zr` and `libarchive` Homebrew formulas, downloads the verified `libarchive-c` binding into Snug's own `vendor/` directory, and validates both optional backends. It does not install the binding into Homebrew's shared Python environment.

By default, application files live in `~/.local/share/snug` and the launcher is `~/.local/bin/snug`. If the launcher directory is absent from `PATH`, add this to your shell profile (`~/.zshrc` for zsh or the appropriate Bash profile), then open a new terminal:

```bash
export PATH="$HOME/.local/bin:$PATH"
snug --version
```

For a custom prefix or Homebrew executable, download and run the installer with environment variables:

```bash
curl -fsSL https://raw.githubusercontent.com/yklucz/snug/main/install.sh -o /tmp/snug-install.sh
SNUG_PREFIX="$HOME/apps/snug" SNUG_BREW="/opt/homebrew/bin/brew" bash /tmp/snug-install.sh
export PATH="$HOME/apps/snug/bin:$PATH"
```

`SNUG_PREFIX` defaults to `~/.local`. `SNUG_BREW` must point to an executable. The launcher retains the chosen application path; retain a custom `SNUG_BREW` in your environment if it cannot otherwise be discovered.

## Linux

The managed installer uses Linuxbrew even if a distribution Python or libarchive is already installed. Install Homebrew for Linux using `https://brew.sh`, then use the same installer and default paths as on macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/yklucz/snug/main/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
snug --version
```

For a source installation without Linuxbrew, use your distribution's Python and native libarchive packages as described below.

## Windows

Run in PowerShell:

```powershell
irm https://raw.githubusercontent.com/yklucz/snug/windows/install.ps1 | iex
snug --version
```

Application files live under `%LOCALAPPDATA%\Snug`. The installer creates `bin\snug.cmd`, appends that directory to your user `Path`, and updates the current PowerShell session. Open a new terminal to refresh other sessions.

The launcher first looks for a compatible Python in Snug's `python\` directory, recorded runtime state, `PATH`, or the `py` launcher. It excludes Windows Store execution aliases. If no compatible interpreter is found, it downloads the embedded runtime described in [Runtime management](runtime.md). Managed dependencies are unpacked into application-owned directories without installing pip into that runtime.

On ARM64 Windows, the installer still selects x64 Python and x64 DLLs; it has no native ARM64 artifact set. It can work only where the operating system supports running those x64 components. An existing ARM64 Python does not satisfy the managed launcher's platform check. ARM64 Windows is not covered by the repository's CI matrix.

## Source installation and pip extras

Clone the repository, create an environment, and install from its root. On macOS/Linux:

```bash
git clone https://github.com/yklucz/snug.git
cd snug
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[all]'
snug --version
```

On Windows, clone the Windows branch and enter the checkout:

```powershell
git clone --branch windows https://github.com/yklucz/snug.git
cd snug
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install '.[all]'
.\.venv\Scripts\snug.exe --version
```

Using the environment's executable directly avoids relying on activation or `PATH`. You can also run `python snug.py` from a source checkout. Source/pip entry points do not invoke the managed launcher or automatically repair dependencies.

Select an extra instead of `all` if fewer backends are needed:

| Install command, from the repository root | Python dependencies | Capability |
|---|---|---|
| `python -m pip install .` | None | ZIP, TAR variants, standalone streams |
| `python -m pip install '.[7z]'` | `py7zr>=1.1.3,<2` | Adds 7z creation and extraction |
| `python -m pip install '.[extended]'` | `libarchive-c>=5.3,<6` | Adds libarchive formats, including 7z extraction |
| `python -m pip install '.[all]'` | Both optional dependencies | All implemented backends |

The `libarchive-c` distribution imports as `libarchive`; it does not supply the native shared library. Installing a pip extra alone is insufficient for libarchive formats. See [Supported formats](supported-formats.md) for the capability matrix.

## Native libarchive for source installations

### Homebrew on macOS or Linux

```bash
brew install libarchive
python -m pip install '.[extended]'
```

On macOS, Snug searches `/opt/homebrew/opt/libarchive/lib/libarchive.dylib` and `/usr/local/opt/libarchive/lib/libarchive.dylib` unless `LIBARCHIVE` is set. For another Homebrew prefix, or Linuxbrew, set the library path before running Snug:

On macOS:

```bash
export LIBARCHIVE="$(brew --prefix libarchive)/lib/libarchive.dylib"
```

On Linuxbrew:

```bash
export LIBARCHIVE="$(brew --prefix libarchive)/lib/libarchive.so"
```

### Debian / Ubuntu

```bash
sudo apt-get update
sudo apt-get install -y libarchive-dev
python -m pip install '.[extended]'
```

`libarchive-dev` brings in the release-specific shared-library package and matches the Linux CI setup. Available readers and codecs depend on the distribution's library build and version.

### Fedora

```bash
sudo dnf install libarchive
python -m pip install '.[extended]'
```

### Arch Linux

```bash
sudo pacman -Syu libarchive
python -m pip install '.[extended]'
```

Package references: [Ubuntu](https://packages.ubuntu.com/noble-updates/libarchive-dev), [Debian](https://packages.debian.org/stable/libarchive-dev), [Fedora](https://packages.fedoraproject.org/pkgs/libarchive/libarchive/), and [Arch](https://archlinux.org/packages/core/x86_64/libarchive/).

### Windows DLL setup

For a source installation, provide a libarchive DLL and all of its dependent DLLs matching your Python architecture. Set `LIBARCHIVE` to the full library filename, and put its directory on `PATH` before running Python:

```powershell
$env:LIBARCHIVE = 'C:\tools\libarchive\libarchive-13.dll'
$env:Path = 'C:\tools\libarchive;' + $env:Path
.\.venv\Scripts\python.exe -m pip install '.[extended]'
.\.venv\Scripts\snug.exe info archive.rar
```

Replace the example directory with your actual DLL location. The filename may differ between builds. Snug also registers the directory containing an explicit `LIBARCHIVE` with Python's Windows DLL search API. Setting only the library filename does not supply missing dependent DLLs.

## Repair, reinstall, and offline use

Managed launchers check both optional backends at startup, including for native-format commands. Missing or unusable dependencies trigger repair, which needs a working internet connection and, on macOS/Linux, a working Homebrew installation. A successful startup check uses local files and does not need a network connection.

Rerun the appropriate installation command above to reinstall application files. Installers validate a staged copy before replacing an existing installation and restore the previous copy if the final startup check fails. This differs from dependency repair, which repairs the installed runtime in place.

See [Runtime management](runtime.md) for checks and integrity boundaries, or [Troubleshooting](troubleshooting.md) for failed repairs and command discovery.
