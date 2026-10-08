#!/bin/bash
# Install Receipt Bridge on a Mac. Paste this into Terminal:
#
#   curl -fsSL https://raw.githubusercontent.com/oliverh57/receipt-bridge/main/install.sh | bash
#
# It downloads the app into ~/Receipt Bridge, then runs the installer in
# that folder (Install Receipt Bridge.command), which sets up Python and
# puts Receipt Bridge in Applications. Running it again is safe: an existing
# copy is kept (updates arrive inside the app) and only rebuilt.
#
# RB_DIR=… installs somewhere else. RB_TARBALL=… (a URL, file:// works)
# installs that code instead of GitHub's, for testing this script.

set -euo pipefail

REPO="oliverh57/receipt-bridge"
DIR="${RB_DIR:-$HOME/Receipt Bridge}"
TARBALL="${RB_TARBALL:-https://github.com/$REPO/archive/refs/heads/main.tar.gz}"

say() { printf '%s\n' "$*"; }
stop() { say ""; say "Receipt Bridge wasn't installed: $*"; exit 1; }

[ "$(uname -s)" = "Darwin" ] || stop "it's a Mac app, and this isn't a Mac."

say "Receipt Bridge"
say ""

# The developer tools (clang, swiftc) build the app on this Mac. Installing
# them is Apple's own window and takes a few minutes.
if ! xcode-select -p >/dev/null 2>&1; then
  xcode-select --install >/dev/null 2>&1 || true
  stop "macOS needs its developer tools first. Click Install in the window that just opened, wait for it to finish, then paste the same command again."
fi

if [ -f "$DIR/app/mac_app.py" ]; then
  say "Already downloaded to $DIR. Keeping it (updates arrive inside the app) and rebuilding."
else
  if [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
    stop "$DIR already exists and isn't Receipt Bridge. Move it aside, or install elsewhere with RB_DIR=/some/folder."
  fi
  say "Downloading to ${DIR}…"
  mkdir -p "$DIR"
  if ! curl -fsSL "$TARBALL" | tar -xz -C "$DIR" --strip-components 1; then
    rm -rf "$DIR"
    stop "couldn't download it. Check the internet connection and try again."
  fi
fi

say ""
exec bash "$DIR/Install Receipt Bridge.command" </dev/null
