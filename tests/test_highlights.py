"""Highlight boxes: found from a receipt's current values and the OCR
layout, so a correction moves or removes its box. Made-up layout."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.highlights import locate  # noqa: E402


def row(*pieces):
    return {"page": 0, "pieces": [{"t": t, "b": b} for t, b in pieces]}


LAYOUT = [
    row(("EXAMPLE CAFE", [0.2, 0.05, 0.6, 0.05])),
    row(("VAT No: GB 123 4567 89", [0.2, 0.12, 0.5, 0.02])),
    row(("12/03/2026", [0.1, 0.2, 0.2, 0.02]), ("11:32", [0.6, 0.2, 0.1, 0.02])),
    row(("Latte", [0.1, 0.3, 0.2, 0.02]), ("£3.40", [0.7, 0.3, 0.1, 0.02])),
    row(("TOTAL", [0.1, 0.4, 0.2, 0.02]), ("£3.40", [0.7, 0.4, 0.1, 0.02])),
]


def boxes(**values):
    found = locate(LAYOUT, **{"supplier": None, "total": None, "when": None, "vat_number": None, "currency": "GBP", **values})
    return {h["field"]: h["box"] for h in found}


def test_each_value_is_boxed_where_it_was_read() -> None:
    b = boxes(supplier="Example Cafe", total=3.40, when="2026-03-12", vat_number="GB123456789")
    assert b["supplier"] == [0.2, 0.05, 0.6, 0.05]
    assert b["total"] == [0.1, 0.4, 0.7, 0.02], "the TOTAL line, not the item at the same price"
    assert b["date"] == [0.1, 0.2, 0.2, 0.02], "just the date, not the time"
    assert b["vat_number"] == [0.2, 0.12, 0.5, 0.02]


def test_a_corrected_value_that_isnt_printed_has_no_box() -> None:
    assert boxes(total=9.99, when="2026-01-01", supplier="Other Shop") == {}


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func(); print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1; print(f"  FAIL  {name}: {exc}")
    print("\nAll tests passed." if not failures else f"\n{failures} test(s) failed.")
    sys.exit(1 if failures else 0)
