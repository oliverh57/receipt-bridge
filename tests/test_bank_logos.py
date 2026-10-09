"""Bank icons: fetched from the bank's own site the first time, then kept.
A fake website; no network."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import bank_logos  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 200
PAGE = b'<html><head><link rel="apple-touch-icon" sizes="180x180" href="/icons/touch.png"></head></html>'


def fake_site(asked: list[str]):
    def get(url: str, timeout: float = 8):
        asked.append(url)
        if url == "https://www.lloydsbank.com/":
            return PAGE, url, "text/html"
        if url == "https://www.lloydsbank.com/icons/touch.png":
            return PNG, url, "image/png"
        raise OSError("not found")
    return get


def test_an_icon_is_fetched_once_and_kept() -> None:
    asked: list[str] = []
    original, bank_logos._get = bank_logos._get, fake_site(asked)
    bank_logos._tried.clear()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "bank-logos"
            got = bank_logos.find("lloyds", folder)
            assert got == folder / "lloyds.png" and got.read_bytes() == PNG
            before = len(asked)
            assert bank_logos.find("lloyds", folder) == got and len(asked) == before, "kept, not fetched again"
            assert bank_logos.find("monzo", folder) is None, "a site that fails: the monogram"
            tries = len(asked)
            assert bank_logos.find("monzo", folder) is None and len(asked) == tries, "asked once per run"
            assert bank_logos.find("../etc", folder) is None and len(asked) == tries, "only known banks"
    finally:
        bank_logos._get = original
        bank_logos._tried.clear()


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print("\nAll tests passed." if not failures else f"\n{failures} test(s) failed.")
    sys.exit(1 if failures else 0)
