#!/bin/bash
# Double-click in Finder to install Receipt Bridge into Applications.
# Safe to run again: it updates the app in place, and keeps your receipts.

cd "$(dirname "$0")" || exit 1
PROJECT="$(pwd)"

fail() {
  echo
  echo "Something went wrong: $1"
  osascript -e "display alert \"Receipt Bridge wasn't installed\" message \"$1\" as critical" >/dev/null 2>&1
  read -r -p "Press Return to close this window."
  exit 1
}

echo "Installing Receipt Bridge…"
echo

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c "" >/dev/null 2>&1; then
  xcode-select --install >/dev/null 2>&1
  fail "macOS needs its developer tools first. Accept the install window that just opened, then double-click this installer again."
fi

if [ ! -x "$PROJECT/.venv/bin/python" ]; then
  echo "1/4  Setting up Python…"
  python3 -m venv "$PROJECT/.venv" || fail "Couldn't set up Python in $PROJECT/.venv."
else
  echo "1/4  Python is already set up."
fi

echo "2/4  Installing what the app needs (a few minutes the first time)…"
"$PROJECT/.venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1
"$PROJECT/.venv/bin/pip" install --quiet -r "$PROJECT/requirements.txt" || fail "Couldn't download the app's parts. Check the internet connection and try again."

echo "3/4  Installing the browser used to fetch supplier receipts…"
"$PROJECT/.venv/bin/playwright" install chromium >/dev/null || fail "Couldn't download the browser. Check the internet connection and try again."

echo "4/4  Building the app and putting it in Applications…"
"$PROJECT/.venv/bin/python" "$PROJECT/build_app.py" --install >/dev/null || fail "Couldn't build the app."

echo
echo "Done. Opening Receipt Bridge."
open -a "/Applications/Receipt Bridge.app"
sleep 1
osascript -e 'tell application "Terminal" to close (every window whose name contains "Install Receipt Bridge")' >/dev/null 2>&1 &
exit 0
