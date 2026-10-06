#!/usr/bin/env bash
# Installed beside Snug and reused by its launcher for automatic repair.
set -Eeuo pipefail
APP_DIR="${1:?Snug application directory is required}"
shift
MODE="${1:---run}"
shift || true

die() { printf 'Snug dependency error: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*" >&2; }

find_brew() {
  if [[ -n "${SNUG_BREW:-}" ]]; then
    [[ -x "$SNUG_BREW" ]] || die 'Homebrew executable not found. Please install Homebrew from https://brew.sh and retry.'
    BREW="$SNUG_BREW"
  else
    BREW="$(command -v brew || true)"
    if [[ -z "$BREW" ]]; then
      for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew /home/linuxbrew/.linuxbrew/bin/brew; do
        if [[ -x "$candidate" ]]; then BREW="$candidate"; break; fi
      done
    fi
  fi
  [[ -n "$BREW" ]] || die 'Please install Homebrew (Linuxbrew on Linux) from https://brew.sh, then run the installer or snug again.'
  export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_INSTALL_CLEANUP=1
}

resolve_runtime() {
  local seven native candidate prefix
  SNUG_PYTHON=""
  SNUG_LIBRARY=""
  # The stable opt links follow upgrades. --prefix without a formula reads no
  # package API metadata, so healthy launches do not require network access.
  prefix="$("$BREW" --prefix 2>/dev/null)" || return 1
  seven="$prefix/opt/py7zr"
  native="$prefix/opt/libarchive"
  SNUG_PYTHON="$seven/libexec/bin/python"
  for candidate in "$seven/libexec/bin/python" "$seven/libexec/bin/python3"; do
    if [[ -x "$candidate" ]]; then SNUG_PYTHON="$candidate"; break; fi
  done
  for candidate in "$native/lib/libarchive.dylib" "$native/lib/libarchive.so"; do
    if [[ -f "$candidate" ]]; then SNUG_LIBRARY="$candidate"; break; fi
  done
  export SNUG_LIBRARY PYTHONDONTWRITEBYTECODE=1
  [[ -x "$SNUG_PYTHON" && -n "$SNUG_LIBRARY" ]]
}

probe() {
  [[ -n "${SNUG_PYTHON:-}" ]] && "$SNUG_PYTHON" -B "$APP_DIR/snug_runtime.py" --check "${1:-all}" >/dev/null 2>&1
}

repair() (
  # Serialize installations. A dead owner can leave a directory after power loss.
  local lock="$APP_DIR/.repair-lock" owner attempts=0 work before after added formula brew_root owned_before=0 owned_after=0
  while ! mkdir "$lock" 2>/dev/null; do
    owner="$(cat "$lock/pid" 2>/dev/null || true)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && ! kill -0 "$owner" 2>/dev/null; then
      rm -f "$lock/pid"
      rmdir "$lock" 2>/dev/null || true
      continue
    fi
    ((attempts+=1))
    if [[ -z "$owner" && "$attempts" -ge 5 ]]; then rmdir "$lock" 2>/dev/null || true; fi
    [[ "$attempts" -le 60 ]] || die 'Another dependency repair is still running. Try snug again when it finishes.'
    sleep 1
  done
  # $$ belongs to the parent shell on macOS's bundled Bash 3.2.
  printf '%s\n' "$$" > "$lock/pid"
  work="$(mktemp -d)"
  trap 'rm -rf "$work"; rm -f "$lock/pid"; rmdir "$lock" 2>/dev/null || true' EXIT
  if resolve_runtime && probe; then exit 0; fi
  # Cache only this operation's downloads; never delete an existing Brew cache.
  export HOMEBREW_CACHE="$work/brew-cache"
  brew_root="$("$BREW" --prefix)"
  before="$(du -sk "$brew_root" 2>/dev/null | awk '{print $1}' || true)"
  before="${before:-0}"
  case "$APP_DIR" in "$brew_root"/*) owned_before="$(du -sk "$APP_DIR" | awk '{print $1}')";; esac
  info 'Installing or repairing archive dependencies with Homebrew'
  "$BREW" install py7zr libarchive >&2 || die 'Homebrew installation failed. Check your connection, then run snug again.'
  resolve_runtime || true
  [[ -n "$SNUG_PYTHON" ]] || die 'Homebrew did not provide the py7zr Python environment.'
  if ! "$SNUG_PYTHON" -B -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
    while IFS= read -r formula; do
      [[ "$formula" == python@* ]] || continue
      "$BREW" reinstall "$formula" >&2 || die 'Homebrew Python repair failed. Connect to the internet and retry.'
    done < <("$BREW" deps --direct py7zr)
    "$BREW" reinstall py7zr >&2 || die 'py7zr repair failed. Connect to the internet and retry.'
    resolve_runtime || true
  fi
  "$SNUG_PYTHON" -B "$APP_DIR/snug_runtime.py" --vendor || die 'Binding repair failed. Connect to the internet and retry.'
  for formula in py7zr libarchive; do
    if ! probe "$formula"; then
      "$BREW" reinstall "$formula" >&2 || die "$formula repair failed. Connect to the internet and retry."
    fi
  done
  resolve_runtime || die 'Repaired dependencies could not be located.'
  if ! probe; then
    # Refresh stale formula metadata only when the installed versions fail checks.
    "$BREW" update --quiet >&2 || die 'Homebrew metadata repair failed. Check your connection and retry.'
    "$BREW" reinstall py7zr libarchive >&2 || die 'Archive dependency repair failed. Check your connection and retry.'
    resolve_runtime || die 'Updated archive dependencies could not be located.'
  fi
  if ! probe; then
    # A shared Python/native dependency may itself have been damaged.
    while IFS= read -r formula; do
      [[ -n "$formula" ]] || continue
      "$BREW" reinstall "$formula" >&2 || die 'Shared dependency repair failed. Connect to the internet and retry.'
    done < <("$BREW" deps --union py7zr libarchive)
    resolve_runtime && probe || die 'Dependencies still failed validation after repair.'
  fi
  after="$(du -sk "$brew_root" 2>/dev/null | awk '{print $1}')"
  case "$APP_DIR" in "$brew_root"/*) owned_after="$(du -sk "$APP_DIR" | awk '{print $1}')";; esac
  added=$(( (${after:-0} - before - owned_after + owned_before) * 1024 ))
  ((added >= 0)) || added=0
  "$SNUG_PYTHON" -B "$APP_DIR/snug_runtime.py" --storage "$added"
)

find_brew
if ! resolve_runtime || ! probe; then repair; fi
resolve_runtime || die 'Archive dependencies could not be located.'
if [[ "$MODE" == --prepare ]]; then exit 0; fi
[[ "$MODE" == --run ]] || die 'Unknown launcher mode'
exec "$SNUG_PYTHON" -B "$APP_DIR/snug_runtime.py" --run "$@"
