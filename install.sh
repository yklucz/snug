#!/usr/bin/env bash
set -Eeuo pipefail

RAW_URL="https://raw.githubusercontent.com/yklucz/snug/main/snug.py"
PREFIX="${HOME}/.local"
BIN_DIR="${PREFIX}/bin"
APP_DIR="${PREFIX}/share/snug"
APP_PATH="${APP_DIR}/snug.py"
LAUNCHER="${BIN_DIR}/snug"

die() { printf "error: %s\n" "$*" >&2; exit 1; }
info() { printf "==> %s\n" "$*"; }

command -v curl >/dev/null 2>&1 || die "curl is required"

PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >/dev/null 2>&1; then
      PYTHON="$(command -v "$candidate")"
      break
    fi
  fi
done
[[ -n "$PYTHON" ]] || die "Python 3.10+ is required"

mkdir -p "$BIN_DIR" "$APP_DIR"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

info "Downloading Snug"
curl -fsSL "$RAW_URL" -o "$TMP"
"$PYTHON" -m py_compile "$TMP" || die "downloaded source failed syntax check"
mv "$TMP" "$APP_PATH"
trap - EXIT
chmod 644 "$APP_PATH"

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$PYTHON" "$APP_PATH" "\$@"
EOF
chmod 755 "$LAUNCHER"

"$LAUNCHER" --version >/dev/null
info "Installed Snug to $LAUNCHER"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) printf "\nAdd this to your shell profile if needed:\n  export PATH=\"%s:\$PATH\"\n\n" "$BIN_DIR" ;;
esac

printf "Run: snug\n"