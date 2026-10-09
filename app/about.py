"""Settings → About: the version, the licence file, the EULA, and the
open-source software Receipt Bridge is built on.

The open-source list is read from what is actually installed in this copy's
Python environment (each package's own metadata and licence files), so it
is never a hand-kept list that drifts from requirements.txt.
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EULA = ROOT / "docs" / "EULA.md"
COPYRIGHT = "© 2026 Oliver Hynds"

# Shipped with or fetched by Receipt Bridge, but not Python packages.
OTHER_SOFTWARE = [
    {"name": "Python", "licence": "PSF-2.0", "url": "https://docs.python.org/3/license.html"},
    {"name": "Chromium (through Playwright)", "licence": "BSD-3-Clause",
     "url": "https://chromium.googlesource.com/chromium/src/+/main/LICENSE"},
]
# Tools that set up the environment, not part of what runs.
NOT_SHIPPED = {"pip", "setuptools", "wheel"}
LICENCE_FILE = re.compile(r"(?:^|/)(?:licen[cs]e|copying|notice)[^/]*$", re.IGNORECASE)


def _licence_of(meta: Any) -> str:
    """A package's licence in a few words: its SPDX expression, its short
    License field, or its "License ::" classifier."""
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    field = (meta.get("License") or "").strip()
    if field and "\n" not in field and len(field) <= 60 and field.upper() != "UNKNOWN":
        return field
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::") and "OSI Approved" != classifier.split(" :: ")[-1]:
            return classifier.split(" :: ")[-1].replace(" License", "")
    return "See its licence"


def _url_of(meta: Any) -> str:
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("source", "homepage", "home", "repository", "source code"):
            return url.strip()
    return (meta.get("Home-page") or "").strip()


def open_source() -> list[dict[str, Any]]:
    """Every package in this environment, by name: its version, licence,
    where it lives, and whether its licence text is here to show."""
    found: dict[str, dict[str, Any]] = {}
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name or name.lower() in NOT_SHIPPED or name.lower() in found:
            continue
        files = [f for f in (dist.files or []) if LICENCE_FILE.search(str(f))]
        found[name.lower()] = {"name": name, "version": dist.version, "licence": _licence_of(dist.metadata),
                               "url": _url_of(dist.metadata), "has_text": bool(files)}
    return sorted(found.values(), key=lambda p: p["name"].lower()) + [dict(o, version="", has_text=False)
                                                                       for o in OTHER_SOFTWARE]


def licence_text(name: str, limit: int = 200_000) -> str | None:
    """The licence files a package carries, as text, or None."""
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None
    parts = []
    for file in dist.files or []:
        if not LICENCE_FILE.search(str(file)):
            continue
        try:
            text = Path(dist.locate_file(file)).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            parts.append(text)
    joined = "\n\n".join(parts)
    return joined[:limit] if joined else None


def eula() -> str:
    try:
        return EULA.read_text(encoding="utf-8")
    except OSError:
        return ""


def about(version: str, google_key: bool, freeagent_key: bool, data_dir: Path) -> dict[str, Any]:
    return {
        "name": "Receipt Bridge",
        "version": version,
        "copyright": COPYRIGHT,
        "licence": {"google": google_key, "freeagent": freeagent_key},
        "data_dir": str(data_dir),
        "eula": eula(),
        "open_source": open_source(),
    }
