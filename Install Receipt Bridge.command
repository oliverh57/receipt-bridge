#!/bin/bash
# Double-click in Finder to install Receipt Bridge into Applications.
# Safe to run again: it updates the app in place, and keeps your receipts.
# A new Mac can start from GitHub instead, which downloads this folder and
# runs this file: see install.sh.

cd "$(dirname "$0")" || exit 1
PROJECT="$(pwd)"

fail() {
  echo
  echo "Something went wrong: $1"
  osascript -e "display alert \"Receipt Bridge wasn't installed\" message \"$1\" as critical" >/dev/null 2>&1
  read -r -p "Press Return to close this window." </dev/tty 2>/dev/null
  exit 1
}

# Python 3.10 or newer. macOS's own python3 is 3.9, which the app can't use.
modern() { [ -x "$1" ] && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; }

find_python() {
  local candidate path
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 \
                   /opt/homebrew/bin/python3 /usr/local/bin/python3 \
                   /Library/Frameworks/Python.framework/Versions/Current/bin/python3 python3; do
    path="$(command -v "$candidate" 2>/dev/null)" || continue
    if modern "$path"; then echo "$path"; return 0; fi
  done
  return 1
}

# None on this Mac: fetch one into this folder with uv (astral.sh), which
# needs no admin password and changes nothing outside the folder.
fetch_python() {
  local uv="$PROJECT/.uv/uv"
  if [ ! -x "$uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR="$PROJECT/.uv" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 || return 1
  fi
  UV_PYTHON_INSTALL_DIR="$PROJECT/.python" "$uv" python install 3.13 >/dev/null 2>&1 || return 1
  UV_PYTHON_INSTALL_DIR="$PROJECT/.python" "$uv" python find 3.13 --managed-python
}

echo "Installing Receipt Bridge…"
echo

# clang and swiftc build the app's launcher and its on-device receipt reader.
if ! xcode-select -p >/dev/null 2>&1; then
  xcode-select --install >/dev/null 2>&1
  fail "macOS needs its developer tools first. Accept the install window that just opened, then run this installer again."
fi

if [ -x "$PROJECT/.venv/bin/python" ] && modern "$PROJECT/.venv/bin/python"; then
  echo "1/4  Python is already set up."
else
  echo "1/4  Setting up Python…"
  # RB_FETCH_PYTHON=1 skips this Mac's own Python (testing the download path)
  { [ -z "${RB_FETCH_PYTHON:-}" ] && PYTHON="$(find_python)"; } || {
    echo "     This Mac has no recent Python, so downloading one (about 20 MB)…"
    PYTHON="$(fetch_python)" || fail "Couldn't download Python. Check the internet connection and try again."
  }
  rm -rf "$PROJECT/.venv"
  "$PYTHON" -m venv "$PROJECT/.venv" || fail "Couldn't set up Python in $PROJECT/.venv."
fi

echo "2/4  Installing what the app needs (a few minutes the first time)…"
"$PROJECT/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1
"$PROJECT/.venv/bin/python" -m pip install --quiet -r "$PROJECT/requirements.txt" || fail "Couldn't download the app's parts. Check the internet connection and try again."

echo "3/4  Installing the browser used to fetch supplier receipts…"
"$PROJECT/.venv/bin/python" -m playwright install chromium >/dev/null || fail "Couldn't download the browser. Check the internet connection and try again."

if [ -n "${RB_BUILD_ONLY:-}" ]; then              # testing: build into dist/, leave Applications alone
  echo "4/4  Building the app into dist/…"
  "$PROJECT/.venv/bin/python" "$PROJECT/build_app.py" >/dev/null || fail "Couldn't build the app."
  echo
  echo "Built $PROJECT/dist/Receipt Bridge.app"
  exit 0
fi

echo "4/4  Building the app and putting it in Applications…"
"$PROJECT/.venv/bin/python" "$PROJECT/build_app.py" --install >/dev/null || fail "Couldn't build the app."

echo
echo "Done. Opening Receipt Bridge."
open -a "/Applications/Receipt Bridge.app"
sleep 1
osascript -e 'tell application "Terminal" to close (every window whose name contains "Install Receipt Bridge")' >/dev/null 2>&1 &
exit 0
