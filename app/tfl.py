"""Receipts for TfL contactless travel, made from the bank payment.

TfL sends nothing for pay-as-you-go travel on a contactless card: no email,
no receipt, and its journey history only exists for a card registered with a
TfL account. The bank payment is the only record there is, so Receipt Bridge
prints one from it. The page says plainly that it was made here from the
bank payment, not issued by TfL: it is a record of the payment, nothing more.

Fares are zero-rated for VAT (passenger transport), so the VAT is £0.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import date
from pathlib import Path
from typing import Any

SUPPLIER = "Transport for London"
CATEGORY = "Travel"                     # FreeAgent's own category, matched by name

# "TFL // CARD_PURCHASE///" (Mettle), "TFL TRAVEL CH TFL.GOV.UK/CP",
# "TRANSPORT FOR LONDON": TfL as a word, so "NETFLIX" never is
_TFL = re.compile(r"(?<![A-Z0-9])TFL(?![A-Z0-9])|TRANSPORT FOR LONDON", re.I)

_SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
         color: #1f2933; font-size: 13px; line-height: 1.5; }
  h1 { font-size: 20px; margin: 0 0 2px 0; }
  .sub { color: #52606d; margin: 0 0 22px 0; }
  table { border-collapse: collapse; width: 100%%; }
  td { padding: 8px 0; border-bottom: 1px solid #e4e7eb; vertical-align: top; }
  td:first-child { color: #52606d; font-weight: 600; width: 34%%; }
  tr.total td { font-size: 16px; font-weight: 700; color: #1f2933; border-bottom: 2px solid #1f2933; }
  .note { margin-top: 22px; color: #616e7c; font-size: 11px; }
</style></head><body>
  <h1>Travel receipt</h1>
  <p class="sub">%(supplier)s · contactless pay as you go</p>
  <table>%(rows)s</table>
  <div class="note">%(note)s</div>
</body></html>"""


def is_tfl(description: str | None) -> bool:
    """A bank payment to TfL, by its statement line."""
    return bool(_TFL.search(description or ""))


def money(amount: float, currency: str = "GBP") -> str:
    symbol = _SYMBOLS.get(currency or "GBP")
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency}"


def long_date(day: str) -> str:
    try:
        d = date.fromisoformat(day[:10])
    except ValueError:
        return day
    return f"{d.day} {d:%B %Y}"


def receipt_html(payment: dict[str, Any], account_name: str = "", made_on: date | None = None) -> str:
    """The receipt page for one bank payment to TfL."""
    amount = abs(float(payment["amount"]))
    currency = payment.get("currency") or "GBP"
    statement_line = (payment.get("description") or "").split("//")[0].strip()
    rows = [
        ("Supplier", SUPPLIER),
        ("Date charged", long_date(payment["dated_on"])),
        ("For", "Travel on TfL services (contactless pay as you go)"),
        ("Paid from", account_name),
        ("Bank statement shows", statement_line),
        ("VAT", f"{money(0, currency)} (fares are zero-rated)"),
    ]
    body = "".join(f"<tr><td>{html_lib.escape(k)}</td><td>{html_lib.escape(v)}</td></tr>" for k, v in rows if v)
    body += f'<tr class="total"><td>Total</td><td>{html_lib.escape(money(amount, currency))}</td></tr>'
    note = (f"Made by Receipt Bridge on {long_date((made_on or date.today()).isoformat())} from the bank "
            "payment above, because TfL doesn't issue receipts for contactless travel. It is a record of "
            "that payment, not a document from TfL. TfL usually charges a day's journeys the following "
            "day; if this card is registered with a TfL account, its journey history is there.")
    return PAGE % {"supplier": html_lib.escape(SUPPLIER), "rows": body, "note": html_lib.escape(note)}


def render(pool: Any, payment: dict[str, Any], out_path: Path, account_name: str = "",
           pdf_config: dict[str, Any] | None = None) -> Path:
    """Print the receipt for `payment` to `out_path` (pool: a BrowserPool)."""
    from .pdf import html_to_pdf

    return html_to_pdf(pool, receipt_html(payment, account_name), out_path, pdf_config)
