"""Each bank's own icon, fetched from its official website into
data/bank-logos/ the first time it's shown. Not shipped with the app.
Banks that block the request (Revolut, Bank of Scotland) keep the UI's
monogram.
"""

from __future__ import annotations

import logging
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

SITES = {
    "mettle": "mettle.co.uk", "natwest": "natwest.com", "rbs": "rbs.co.uk", "ulster": "ulsterbank.ie",
    "bankofscotland": "bankofscotland.co.uk", "bankofireland": "bankofireland.com", "barclays": "barclays.co.uk",
    "caterallen": "caterallen.co.uk", "danske": "danskebank.co.uk", "firstdirect": "firstdirect.com",
    "halifax": "halifax.co.uk", "hsbc": "hsbc.co.uk", "lloyds": "lloydsbank.com", "metro": "metrobankonline.co.uk",
    "santander": "santander.co.uk", "starling": "starlingbank.com", "tide": "tide.co", "tsb": "tsb.co.uk",
    "virgin": "virginmoney.com", "wise": "wise.com", "monzo": "monzo.com", "revolut": "revolut.com",
    "capitalontap": "capitalontap.com", "allica": "allica.bank"}
EXTENSIONS = (".svg", ".png", ".ico", ".jpg")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
      "Accept-Language": "en-GB"}

_tried: set[str] = set()          # once per run: an offline Mac or a blocking bank isn't asked again
_locks = {key: threading.Lock() for key in SITES}


def _get(url: str, timeout: float = 8) -> tuple[bytes, str, str]:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read(2 * 1024 * 1024), r.geturl(), r.headers.get("Content-Type", "")


def _candidates(domain: str) -> list[tuple[int, str]]:
    """The site's declared icons, biggest first, then the usual fallbacks."""
    html, final = "", f"https://www.{domain}/"
    for home in (f"https://www.{domain}/", f"https://{domain}/"):
        try:
            data, final, _ = _get(home)
            html = data.decode("utf8", "ignore")
            break
        except Exception:
            continue
    else:
        return []
    found = []
    for tag in re.findall(r"<link\b[^>]*>", html, re.I):
        rel = re.search(r'rel=["\']([^"\']+)', tag, re.I)
        href = re.search(r'href=["\']([^"\']+)', tag, re.I)
        if not rel or not href or "icon" not in rel.group(1).lower() or "mask" in rel.group(1).lower():
            continue
        if href.group(1).startswith("data:"):
            continue
        size = re.search(r'sizes=["\'](\d+)x', tag, re.I)
        found.append((int(size.group(1)) if size else 180 if "apple" in rel.group(1).lower() else 0,
                      urllib.parse.urljoin(final, href.group(1))))
    found += [(150, urllib.parse.urljoin(final, "/apple-touch-icon.png")),
              (1, urllib.parse.urljoin(final, "/favicon.ico"))]
    return sorted(found, key=lambda c: -c[0])


def _extension(data: bytes, url: str, content_type: str) -> str | None:
    if b"<svg" in data[:500] or "svg" in content_type:
        return ".svg"
    if data[:4] == b"\0\0\1\0" or url.split("?")[0].endswith(".ico"):
        return ".ico"
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    return None


def fetch(key: str, folder: Path) -> Path | None:
    """Download one bank's icon into `folder`. None if it can't be had."""
    if key not in SITES:
        return None
    for _size, url in _candidates(SITES[key]):
        try:
            data, _, content_type = _get(url)
        except Exception:
            continue
        if len(data) < 100 or b"<html" in data[:200].lower():
            continue
        ext = _extension(data, url, content_type)
        if not ext:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{key}{ext}"
        temp = folder / f".{key}{ext}.part"
        temp.write_bytes(data)
        temp.replace(target)
        return target
    return None


def find(key: str, folder: Path) -> Path | None:
    """The bank's icon, fetched the first time it's asked for."""
    if key not in SITES:
        return None
    def have() -> Path | None:
        return next((folder / f"{key}{ext}" for ext in EXTENSIONS if (folder / f"{key}{ext}").is_file()), None)
    found = have()
    if found or key in _tried:
        return found
    with _locks[key]:              # the same bank in several rows asks once
        if key not in _tried:
            _tried.add(key)
            try:
                fetch(key, folder)
            except Exception as exc:
                log.info("bank logo %s: %s", key, exc)
    return have()
