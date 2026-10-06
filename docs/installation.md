# Installation

[← Back to README](../README.md)

On `main`, Snug supports macOS and Linux through explicit CLI commands and a terminal TUI. Windows setup is maintained on the [`windows` branch](https://github.com/yklucz/snug/tree/windows); use its [installation guide](https://github.com/yklucz/snug/blob/windows/docs/installation.md).

Choose a managed installer for automatic backend setup and dependency repair, or install from source to manage Python and dependencies yourself. Managed installation and dependency downloads require internet access. A healthy installation can work offline.

## Requirements

- Source installations require Python 3.10 or newer, as declared in [pyproject.toml](../pyproject.toml). The base package has no third-party Python dependencies.
- The macOS/Linux installer requires Bash, `curl`, and Homebrew (Linuxbrew on Linux). It uses Homebrew's Python environment for `py7zr`.
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

## Source installation and pip extras

Clone `main`, create an environment, and install from its root:

```bash
git clone https://github.com/yklucz/snug.git
cd snug
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[all]'
snug --version
```

You can also run `python snug.py` from a source checkout. Source/pip entry points use their chosen environment and do not invoke the managed launcher or automatically repair dependencies.

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

## Repair, reinstall, and offline use

Managed launchers check both optional backends at startup, including for native-format commands. Missing or unusable dependencies trigger repair, which needs a working internet connection and, on macOS/Linux, a working Homebrew installation. A successful dependency startup check uses local files and does not need a network connection. The CLI may make a best-effort periodic release check; archive operations continue offline. See [Updates](updates.md) to disable checks or check explicitly.

Use `snug update` for supported installer-managed updates, or rerun the installation command above to reinstall application files. See [Updates](updates.md) for installation ownership and release-asset requirements. Installers validate a staged copy before replacing an existing installation and restore the previous copy if the final startup check fails. This differs from dependency repair, which repairs the installed runtime in place.

See [Runtime management](runtime.md) for checks and integrity boundaries, or [Troubleshooting](troubleshooting.md) for failed repairs and command discovery.
