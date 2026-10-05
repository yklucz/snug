#!/usr/bin/env bash
set -Eeuo pipefail

RAW_BASE="https://raw.githubusercontent.com/yklucz/snug/main"
PREFIX="${SNUG_PREFIX:-${HOME}/.local}"
BIN_DIR="${PREFIX}/bin"
APP_DIR="${PREFIX}/share/snug"
LAUNCHER="${BIN_DIR}/snug"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

command -v curl >/dev/null 2>&1 || die 'curl is required'
PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
      PYTHON="$(command -v "$candidate")"
      break
    fi
  fi
done
[[ -n "$PYTHON" ]] || die 'Python 3.10+ is required'

mkdir -p "$BIN_DIR" "$APP_DIR"
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
info 'Downloading Snug'
for source in snug.py snug_core.py snug_ext.py; do
  curl -fsSL "$RAW_BASE/$source" -o "$STAGING/$source"
  "$PYTHON" -m py_compile "$STAGING/$source" || die 'downloaded source failed syntax check'
done

# Keep dependencies isolated from system Python; never use sudo or compile them.
"$PYTHON" -m venv "$APP_DIR/venv" || die 'Python venv support is required (Debian/Ubuntu: install python3-venv)'
APP_PYTHON="$APP_DIR/venv/bin/python"
if ! "$APP_PYTHON" -m pip install --disable-pip-version-check --only-binary=:all: 'libarchive-c>=5.3,<6' 'py7zr>=1.1.3,<2'; then
  info 'Optional backends could not be installed. Native ZIP/TAR and compression streams remain available.'
  printf 'Retry: "%s" -m pip install --only-binary=:all: "libarchive-c>=5.3,<6" "py7zr>=1.1.3,<2"\n' "$APP_PYTHON"
fi
# Verify the complete downloaded set before replacing the installed modules.
"$APP_PYTHON" "$STAGING/snug.py" --version >/dev/null || die 'downloaded Snug failed its startup check'
for source in snug.py snug_core.py snug_ext.py; do
  mv "$STAGING/$source" "$APP_DIR/$source"
  chmod 644 "$APP_DIR/$source"
done
cat > "$LAUNCHER" <<LAUNCH
#!/usr/bin/env bash
exec "$APP_PYTHON" "$APP_DIR/snug.py" "\$@"
LAUNCH
chmod 755 "$LAUNCHER"
"$LAUNCHER" --version

if ! "$APP_PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); from snug_ext import LibarchiveBackend; raise SystemExit(0 if LibarchiveBackend().available() else 1)' "$APP_DIR"; then
  printf '\nlibarchive support unavailable. Install the native library:\n'
  case "$(uname -s)" in
    Darwin) printf '  brew install libarchive\n' ;;
    Linux) printf '  Debian/Ubuntu: sudo apt install libarchive13\n  Fedora: sudo dnf install libarchive\n  Arch: sudo pacman -S libarchive\n' ;;
  esac
  printf 'If needed, set LIBARCHIVE to the full path of the shared library.\n'
fi
info "Installed Snug to $LAUNCHER"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) printf '\nAdd this to your shell profile if needed:\n  export PATH="%s:$PATH"\n\n' "$BIN_DIR" ;;
esac
printf 'Run: snug\n'
