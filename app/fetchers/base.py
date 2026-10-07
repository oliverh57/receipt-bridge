"""Contract every PDF fetcher implements.

A fetcher's job is to obtain the vendor's *own* receipt document, which is
always better paperwork than a printed email. Fetchers are allowed to fail —
returning None is a normal outcome, and the pipeline falls back to rendering
the email. They must never raise past `fetch`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..browser import BrowserPool
from ..email_message import Email


@dataclass
class FetchContext:
    """Everything a fetcher is given to do its work."""

    pool: BrowserPool
    message: Email
    values: dict[str, Any]
    options: dict[str, Any]
    out_path: Path
    debug_dir: Path
    timeout_seconds: int = 45
    pdf_config: dict[str, Any] | None = None

    def option(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def url_from_field(self, default_field: str) -> str | None:
        """Resolve the link the watcher pointed this fetcher at."""
        field_name = self.option("url_field", default_field)
        value = self.values.get(field_name)
        return str(value) if value else None

    @property
    def timeout_ms(self) -> int:
        return self.timeout_seconds * 1000


@dataclass
class FetchResult:
    path: Path
    source: str
    note: str = ""


class Fetcher(Protocol):
    id: str

    def fetch(self, ctx: FetchContext) -> FetchResult | None:
        ...
