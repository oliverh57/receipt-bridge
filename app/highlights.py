"""Where on the photo each value was read: boxes to highlight (PLAN.md §11).

The OCR helper records every row's pieces with their position (`layout`).
Given a receipt's current values (supplier, date, total, VAT number), this
finds the piece or row they came from. Worked out from the current values,
so a correction moves (or removes) its box. Pure; no OCR here.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .receipt_text import find_dates

TOTAL_WORDS = re.compile(r"total|balance|amount|due|to pay|paid|summe|totaal|totale|importe|montant|合計", re.I)


def _union(boxes: list[list[float]]) -> list[float]:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]


def _row_text(row: dict[str, Any]) -> str:
    return "   ".join(p["t"] for p in row.get("pieces", []))


def _amount_patterns(total: float) -> list[str]:
    whole = f"{abs(total):.2f}"
    return [whole, whole.replace(".", ",")]


def _has_amount(text: str, total: float) -> bool:
    for pattern in _amount_patterns(total):
        if re.search(rf"(?<![\d.,]){re.escape(pattern)}(?!\d)", text):
            return True
    return False


def locate(layout: list[dict[str, Any]], *, supplier: str | None, total: float | None,
           when: str | None, vat_number: str | None, currency: str | None = None) -> list[dict[str, Any]]:
    """[{field, label, page, box}], box = [x, y, w, h] as fractions of the page."""
    rows = [r for r in layout or [] if r.get("pieces")]
    found: list[dict[str, Any]] = []

    def add(field: str, label: str, row: dict[str, Any], pieces: list[dict[str, Any]] | None = None) -> None:
        boxes = [p["b"] for p in (pieces or row["pieces"])]
        found.append({"field": field, "label": label, "page": int(row.get("page", 0)), "box": _union(boxes)})

    # supplier: a row near the top that names it
    if supplier and supplier != "Unknown supplier":
        words = [w for w in re.findall(r"[A-Za-z]{3,}", supplier)]
        if words:
            key = words[0].lower()
            for row in rows[:12]:
                hit = [p for p in row["pieces"] if key in p["t"].lower().replace("’", "'")]
                if hit:
                    add("supplier", "Supplier", row, hit)
                    break

    # total: the row with the amount and a total word, else the first with the amount
    if total is not None:
        with_amount = [r for r in rows if _has_amount(_row_text(r), float(total))]
        labelled = [r for r in with_amount if TOTAL_WORDS.search(_row_text(r))]
        row = (labelled or with_amount or [None])[0]
        if row:
            add("total", "Total", row)

    # date: the piece(s) holding that date
    if when:
        try:
            day = date.fromisoformat(when[:10])
        except ValueError:
            day = None
        if day:
            for row in rows:
                if day not in find_dates(_row_text(row), currency):
                    continue
                hit = [p for p in row["pieces"] if day in find_dates(p["t"], currency)]
                add("date", "Date", row, hit or None)
                break

    # VAT number: the row with its nine digits
    if vat_number:
        digits = re.sub(r"\D", "", vat_number)[:9]
        for row in rows:
            if digits and digits in re.sub(r"\D", "", _row_text(row)):
                add("vat_number", "VAT number", row)
                break
    return found
