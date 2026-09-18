#!/usr/bin/env bash
set -euo pipefail

if (($# == 0)); then
  echo "Usage: with-deno-keyring.sh COMMAND [ARG ...]" >&2
  exit 2
fi

unlock_keyring() {
  umask 077
  mkdir -p "${HOME}/.local/share/keyrings"
  if command -v gnome-keyring-daemon >/dev/null 2>&1; then
    # Headless Termux/Debian has no login manager to unlock the Secret Service.
    # The keyring file remains protected by the user's filesystem permissions.
    printf '\n' | gnome-keyring-daemon --unlock --components=secrets >/dev/null 2>&1 || true
  fi
}

if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  unlock_keyring
  exec "$@"
fi

if command -v dbus-run-session >/dev/null 2>&1 && command -v gnome-keyring-daemon >/dev/null 2>&1; then
  exec dbus-run-session -- bash -c '
    set -euo pipefail
    umask 077
    mkdir -p "${HOME}/.local/share/keyrings"
    printf "\\n" | gnome-keyring-daemon --unlock --components=secrets >/dev/null 2>&1 || true
    exec "$@"
  ' with-deno-keyring "$@"
fi

exec "$@"
