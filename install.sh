#!/usr/bin/env bash
set -Eeuo pipefail

RAW_BASE="${SNUG_RAW_BASE:-https://raw.githubusercontent.com/yklucz/snug/main}"
PREFIX="${SNUG_PREFIX:-${HOME}/.local}"
BIN_DIR="$PREFIX/bin"
APP_DIR="$PREFIX/share/snug"
LAUNCHER="$BIN_DIR/snug"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || die 'curl is required'
case "$(uname -s)" in Darwin|Linux) ;; *) die 'Use install.ps1 on Windows';; esac
mkdir -p "$BIN_DIR" "$PREFIX/share"
STAGING="$(mktemp -d "$PREFIX/share/.snug-install.XXXXXX")"
NEW_LAUNCHER="$(mktemp "$BIN_DIR/.snug-launcher.XXXXXX")"
trap 'rm -rf "$STAGING"; rm -f "$NEW_LAUNCHER"' EXIT
printf '==> Downloading Snug\n'
for source in snug.py snug_core.py snug_ext.py snug_runtime.py runtime.sh runtime-lock.json; do
  curl -fsSL --proto '=https' --proto-redir '=https' --retry 2 "$RAW_BASE/$source" -o "$STAGING/$source"
done
# All backends must validate before replacing an existing working installation.
bash "$STAGING/runtime.sh" "$STAGING" --prepare
bash "$STAGING/runtime.sh" "$STAGING" --run --version >/dev/null
if [[ -d "$APP_DIR" ]]; then
  [[ ! -e "$APP_DIR.previous" ]] || die 'An earlier installation backup exists; move it aside before reinstalling.'
  mv "$APP_DIR" "$APP_DIR.previous"
fi
if ! mv "$STAGING" "$APP_DIR"; then
  [[ ! -d "$APP_DIR.previous" ]] || mv "$APP_DIR.previous" "$APP_DIR"
  die 'Could not replace the installed application'
fi
# %q preserves spaces and shell metacharacters in a custom installation prefix.
printf '#!/usr/bin/env bash\nexec bash %q %q --run "$@"\n' "$APP_DIR/runtime.sh" "$APP_DIR" > "$NEW_LAUNCHER"
chmod 755 "$NEW_LAUNCHER"
if ! "$NEW_LAUNCHER" --version; then
  mv "$APP_DIR" "$STAGING"
  [[ ! -d "$APP_DIR.previous" ]] || mv "$APP_DIR.previous" "$APP_DIR"
  die 'Installed startup check failed; the previous installation was restored'
fi
mv "$NEW_LAUNCHER" "$LAUNCHER"
[[ ! -d "$APP_DIR.previous" ]] || rm -rf "$APP_DIR.previous"
printf '==> Installed Snug to %s\n' "$LAUNCHER"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) printf '\nAdd this to your shell profile if needed:\n  export PATH="%s:$PATH"\n\n' "$BIN_DIR" ;;
esac
printf 'Run: snug\n'
