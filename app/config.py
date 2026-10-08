"""Configuration loading and path resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _resolve(value: str) -> Path:
    """Expand ~ and make relative paths relative to the project root."""
    path = Path(os.path.expanduser(str(value)))
    return path if path.is_absolute() else (ROOT / path)


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def data_dir(self) -> Path:
        return _resolve(self.raw.get("data_dir", "data"))

    @property
    def export_dir(self) -> Path:
        return _resolve(self.raw.get("export_dir", "~/ReceiptBridge/to-freeagent"))

    @property
    def watchers_dir(self) -> Path:
        return _resolve(self.raw.get("watchers_dir", "watchers"))

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"

    @property
    def debug_dir(self) -> Path:
        return self.data_dir / "debug"

    @property
    def accounts_dir(self) -> Path:
        """One Gmail token file per connected account."""
        return self.data_dir / "accounts"

    @property
    def browser_profile_dir(self) -> Path:
        """Isolated Chrome profile for the app window, never your own."""
        return self.data_dir / "browser-profile"

    @property
    def photo_inbox(self) -> Path:
        """Where the iPhone Shortcut drops receipt photos (PLAN.md §4.2).
        iCloud is only a drop-off: each file is moved out as soon as it's read."""
        return _resolve(self.raw.get(
            "photo_inbox", "~/Library/Mobile Documents/com~apple~CloudDocs/Receipt Inbox"))

    @property
    def photos_dir(self) -> Path:
        """The app's own copy of every original photo, once it leaves iCloud."""
        return self.data_dir / "photos"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "receipts.sqlite3"

    @property
    def gmail(self) -> dict[str, Any]:
        return self.raw.get("gmail", {})

    @property
    def credentials_file(self) -> Path:
        return _resolve(self.gmail.get("credentials_file", "credentials.json"))

    @property
    def token_file(self) -> Path:
        return _resolve(self.gmail.get("token_file", "data/token.json"))

    @property
    def scopes(self) -> list[str]:
        return list(
            self.gmail.get("scopes", ["https://www.googleapis.com/auth/gmail.readonly"])
        )

    @property
    def initial_lookback_days(self) -> int:
        return int(self.gmail.get("initial_lookback_days", 90))

    @property
    def max_results_per_scan(self) -> int:
        return int(self.gmail.get("max_results_per_scan", 100))

    @property
    def pdf(self) -> dict[str, Any]:
        return self.raw.get("pdf", {})

    @property
    def fetchers(self) -> dict[str, Any]:
        return self.raw.get("fetchers", {})

    @property
    def fetchers_enabled(self) -> bool:
        return bool(self.fetchers.get("enabled", True))

    @property
    def headless(self) -> bool:
        return bool(self.fetchers.get("headless", True))

    @property
    def fetch_timeout(self) -> int:
        return int(self.fetchers.get("timeout_seconds", 45))

    @property
    def debug_on_failure(self) -> bool:
        return bool(self.fetchers.get("debug_on_failure", True))

    @property
    def web(self) -> dict[str, Any]:
        return self.raw.get("web", {})

    @property
    def freeagent_credentials_file(self) -> Path:
        """FreeAgent OAuth app id and secret: Receipt Bridge's own, shipped with it."""
        return _resolve(self.raw.get("freeagent", {}).get(
            "credentials_file", "freeagent_credentials.json"))

    @property
    def freeagent_token_file(self) -> Path:
        return self.data_dir / "freeagent" / "token.json"

    @property
    def freeagent_redirect_uri(self) -> str:
        """Where FreeAgent sends the browser after sign-in: this app's own
        server, so sign-in works while the app is running."""
        port = self.web.get("port", 8765)
        return f"http://127.0.0.1:{port}/freeagent/callback"

    @property
    def update_repo(self) -> str:
        """The GitHub repository (owner/name) checked for newer versions."""
        from .updates import DEFAULT_REPO
        return str(self.raw.get("updates", {}).get("repo") or DEFAULT_REPO)

    @property
    def shortcut_url(self) -> str:
        """The iPhone "Receipt" Shortcut's iCloud link, offered by the setup guide."""
        from .setup_guide import SHORTCUT_URL
        return str(self.raw.get("iphone", {}).get("shortcut_url") or SHORTCUT_URL)

    @property
    def setup_folders(self) -> tuple[Path | None, Path | None]:
        """Stand-ins for iCloud Drive and its Shortcuts folder, for a demo
        copy (`setup: {icloud_drive, shortcuts_folder}`). None: the real ones."""
        setup = self.raw.get("setup", {})
        return tuple(_resolve(setup[k]) if setup.get(k) else None for k in ("icloud_drive", "shortcuts_folder"))

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.pdf_dir, self.debug_dir, self.export_dir, self.photos_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_config(path: Path | str | None = None) -> Config:
    config_path = Path(path) if path else ROOT / "config.yaml"
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return Config(raw=raw)
