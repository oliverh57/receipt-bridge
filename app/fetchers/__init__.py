"""Fetcher registry.

Watchers name a fetcher by id (`pdf: [{fetcher: trainline}]`). Adding one means
writing a module here that calls `register`, then importing it below.
"""

from __future__ import annotations

from .base import FetchContext, Fetcher, FetchResult

_REGISTRY: dict[str, Fetcher] = {}


def register(fetcher: Fetcher) -> Fetcher:
    _REGISTRY[fetcher.id] = fetcher
    return fetcher


def get(fetcher_id: str) -> Fetcher | None:
    return _REGISTRY.get(fetcher_id)


def available() -> list[str]:
    return sorted(_REGISTRY)


from . import trainline  # noqa: E402,F401  (import registers the fetcher)

__all__ = ["FetchContext", "FetchResult", "Fetcher", "register", "get", "available"]
