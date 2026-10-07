"""What the first-run setup guide needs beyond the normal settings.

* Where iCloud Drive is, and whether the receipt inbox is inside it.
* The iPhone Shortcut's settings file. The Shortcut saves into iCloud Drive
  at a path it reads from `Receipt Bridge.txt` in iCloud Drive's Shortcuts
  folder (the folder Shortcuts' Get File looks in by default). This app
  writes that file whenever the inbox changes, so the Shortcut follows the
  Mac's choice with no setup on the phone. If the file isn't there, the
  Shortcut asks once and writes it itself (SETUP-FOR-YOU.md §2).
* A QR code for the Shortcut's iCloud link, drawn by macOS's own CoreImage.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

ICLOUD_DRIVE = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
# iCloud Drive › Shortcuts, as the Mac sees it
SHORTCUTS_FOLDER = Path.home() / "Library" / "Mobile Documents" / "iCloud~is~workflow~my~workflows" / "Documents"
SHORTCUT_SETTINGS = "Receipt Bridge.txt"
INBOX_NAME = "Receipt Inbox"
SUBFOLDERS = ("Bank", "Expense")

# The "Receipt" Shortcut's iCloud link (Shortcuts → Share → Copy iCloud
# Link). Shipped here so every copy can offer it; config.yaml's
# `iphone.shortcut_url` overrides it. Empty: the guide shows how to build it.
SHORTCUT_URL = ""


def icloud_available() -> bool:
    return ICLOUD_DRIVE.is_dir()


def icloud_relative(path: Path) -> str | None:
    """The inbox as the Shortcut sees it, e.g. "Receipt Inbox" or
    "Work/Receipt Inbox"; None when it isn't in iCloud Drive."""
    try:
        rel = path.resolve().relative_to(ICLOUD_DRIVE.resolve())
    except (ValueError, OSError):
        return None
    return rel.as_posix() if rel.parts else None


def inbox_for(location: str) -> Path:
    """The inbox folder for a place chosen in the guide: "icloud", or a
    folder from the Mac's picker. A folder that already is an inbox (named
    Receipt Inbox, or holding Bank/ and Expense/) is used as it is; any
    other gets a Receipt Inbox inside it."""
    if location == "icloud":
        if not icloud_available():
            raise ValueError("iCloud Drive is off on this Mac. Turn it on in System Settings, or choose a folder.")
        return ICLOUD_DRIVE / INBOX_NAME
    chosen = Path(location).expanduser()
    if not chosen.is_absolute():
        raise ValueError("Choose a full folder path")
    if not chosen.is_dir():
        raise ValueError(f"No folder at {chosen}")
    if chosen.name == INBOX_NAME or all((chosen / s).is_dir() for s in SUBFOLDERS):
        return chosen
    return chosen / INBOX_NAME


def write_shortcut_settings(inbox: Path) -> str | None:
    """Tell the Shortcut where to save. Returns the path written, relative to
    iCloud Drive, or None when the inbox isn't in iCloud Drive (the file is
    then removed, so the Shortcut asks rather than saving somewhere the Mac
    no longer reads). Never raises: the Mac side works without it."""
    target = SHORTCUTS_FOLDER / SHORTCUT_SETTINGS
    rel = icloud_relative(inbox)
    try:
        if rel is None:
            target.unlink(missing_ok=True)
            return None
        if not SHORTCUTS_FOLDER.is_dir():
            return None                     # Shortcuts isn't syncing with iCloud on this Mac
        if not target.exists() or target.read_text(encoding="utf-8").strip() != rel:
            target.write_text(rel + "\n", encoding="utf-8")
        return rel
    except OSError:
        log.info("couldn't write the Shortcut's settings file", exc_info=True)
        return None


_qr_cache: dict[str, bytes] = {}


def qr_png(text: str) -> bytes:
    """A QR code as a PNG, one pixel per module (scale it up with
    `image-rendering: pixelated`). Raises RuntimeError off macOS."""
    if text in _qr_cache:
        return _qr_cache[text]
    try:
        import AppKit
        import objc
    except ImportError as exc:
        raise RuntimeError("QR codes need macOS") from exc
    found: dict = {}
    objc.loadBundle("CoreImage", found, bundle_path="/System/Library/Frameworks/CoreImage.framework")
    qr = found["CIFilter"].filterWithName_("CIQRCodeGenerator")
    qr.setValue_forKey_(AppKit.NSString.stringWithString_(text).dataUsingEncoding_(AppKit.NSUTF8StringEncoding),
                        "inputMessage")
    qr.setValue_forKey_("M", "inputCorrectionLevel")
    rep = AppKit.NSBitmapImageRep.alloc().initWithCIImage_(qr.outputImage())
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    if data is None:
        raise RuntimeError("Couldn't draw the QR code")
    _qr_cache[text] = bytes(data)
    return _qr_cache[text]
