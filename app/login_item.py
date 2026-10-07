"""Open Receipt Bridge at login: a LaunchAgent that opens the installed app.

Shared by the app menu's "Open at Login" and Settings → General, so both
read the same thing: whether the LaunchAgent file exists.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LABEL = "com.receiptbridge.app"
LAUNCH_AGENT = Path.home() / f"Library/LaunchAgents/{LABEL}.plist"
INSTALLED = Path("/Applications/Receipt Bridge.app")
PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-a</string><string>{app}</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
"""


def bundle_path() -> str | None:
    """The .app to open at login: the one running, else the installed one.
    None when there's no app bundle at all (run from a terminal, not built)."""
    for parent in Path(sys.argv[0]).resolve().parents:
        if parent.suffix == ".app":
            return str(parent)
    return str(INSTALLED) if INSTALLED.is_dir() else None


PROJECT = Path(__file__).resolve().parent.parent


def install_app() -> str:
    """Build the app and put it in /Applications (build_app.py --install).
    Returns the builder's output; raises RuntimeError if it failed."""
    result = subprocess.run([sys.executable, str(PROJECT / "build_app.py"), "--install"],
                            cwd=PROJECT, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not INSTALLED.is_dir():
        raise RuntimeError((result.stderr or result.stdout).strip()[-300:] or "the installer failed")
    return result.stdout


def is_enabled() -> bool:
    return LAUNCH_AGENT.exists()


def set_enabled(on: bool) -> None:
    """Raises RuntimeError when turning on without an installed app
    (ReceiptService.set_open_at_login installs it first)."""
    if not on:
        if LAUNCH_AGENT.exists():
            subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT)], check=False, capture_output=True)
            LAUNCH_AGENT.unlink(missing_ok=True)
        return
    bundle = bundle_path()
    if bundle is None:
        raise RuntimeError("Receipt Bridge isn't in your Applications folder yet")
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT.write_text(PLIST.format(label=LABEL, app=bundle), encoding="utf-8")
    subprocess.run(["launchctl", "load", str(LAUNCH_AGENT)], check=False, capture_output=True)
