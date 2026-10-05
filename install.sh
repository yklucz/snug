#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="snug"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10

usage() {
  cat <<'EOF'
Snug installer

Usage:
  ./install.sh
  ./install.sh --prefix PREFIX
  ./install.sh --uninstall
  ./install.sh --uninstall --prefix PREFIX
  ./install.sh --help

Options:
  --prefix PREFIX   Installation prefix.
                    Default: ~/.local for normal users, /usr/local for root.
  --uninstall       Remove the installed `snug` command.
  -h, --help        Show this help.

Examples:
  ./install.sh
  ./install.sh --prefix /usr/local
  ./install.sh --uninstall
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '==> %s\n' "$*"
}

warn() {
  printf 'warning: %s\n' "$*" >&2
}

PREFIX=""
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      [[ $# -ge 2 ]] || die "--prefix requires a value"
      PREFIX="$2"
      shift 2
      ;;
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

if [[ -z "$PREFIX" ]]; then
  if [[ "$(id -u)" -eq 0 ]]; then
    PREFIX="/usr/local"
  else
    PREFIX="${HOME}/.local"
  fi
fi

BIN_DIR="${PREFIX}/bin"
DEST="${BIN_DIR}/${APP_NAME}"

if [[ "$UNINSTALL" -eq 1 ]]; then
  if [[ -e "$DEST" || -L "$DEST" ]]; then
    info "Removing ${DEST}"
    rm -f "$DEST"
    info "Snug uninstalled."
  else
    info "Snug is not installed at ${DEST}"
  fi
  exit 0
fi

# Resolve the directory containing this installer.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# Support both the development filename and the final project filename.
SOURCE=""
for candidate in \
  "${SCRIPT_DIR}/snug.py" \
  "${SCRIPT_DIR}/snug_fixed.py" \
  "${SCRIPT_DIR}/snug"
do
  if [[ -f "$candidate" ]]; then
    SOURCE="$candidate"
    break
  fi
done

[[ -n "$SOURCE" ]] || die \
  "could not find snug.py, snug_fixed.py, or snug next to install.sh"

# Locate Python.
PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" - <<PY >/dev/null 2>&1
import sys
raise SystemExit(
    0 if sys.version_info >= (${MIN_PYTHON_MAJOR}, ${MIN_PYTHON_MINOR}) else 1
)
PY
    then
      PYTHON="$(command -v "$candidate")"
      break
    fi
  fi
done

[[ -n "$PYTHON" ]] || die \
  "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ is required"

info "Using Python: ${PYTHON}"
info "Checking source syntax"

"$PYTHON" -m py_compile "$SOURCE" || die "Python syntax check failed"

# Make sure the destination directory exists.
if ! mkdir -p "$BIN_DIR" 2>/dev/null; then
  die "cannot create ${BIN_DIR}; try a user-writable --prefix or run with appropriate privileges"
fi

# Install atomically so a failed copy never leaves a partial executable.
TMP="${DEST}.tmp.$$"
cleanup() {
  rm -f "$TMP"
}
trap cleanup EXIT

info "Installing ${APP_NAME} to ${DEST}"

{
  printf '#!%s\n' "$PYTHON"
  # Remove an existing shebang from the source, if present.
  if head -n 1 "$SOURCE" | grep -q '^#!'; then
    tail -n +2 "$SOURCE"
  else
    cat "$SOURCE"
  fi
} > "$TMP"

chmod 755 "$TMP"
mv -f "$TMP" "$DEST"

# The move succeeded, so the temp path no longer exists.
trap - EXIT

info "Verifying installation"
if ! "$DEST" --version >/dev/null 2>&1; then
  rm -f "$DEST"
  die "installed command failed its startup check"
fi

VERSION="$("$DEST" --version 2>/dev/null || true)"
if [[ -n "$VERSION" ]]; then
  info "Installed: ${VERSION}"
else
  info "Installed ${APP_NAME}"
fi

case ":${PATH}:" in
  *":${BIN_DIR}:"*)
    ;;
  *)
    warn "${BIN_DIR} is not currently in PATH"
    printf '\nAdd this to your shell profile:\n\n'
    printf '  export PATH="%s:$PATH"\n\n' "$BIN_DIR"
    ;;
esac

printf '\nRun:\n  %s\n\n' "$APP_NAME"
