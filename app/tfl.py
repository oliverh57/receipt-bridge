"""Receipts for TfL contactless travel, made from the bank payment.

TfL sends nothing for pay-as-you-go travel on a contactless card: no email,
no receipt, and its journey history only exists for a card registered with a
TfL account. The bank payment is the only record there is, so Receipt Bridge
prints one from it, laid out as a formal receipt (number, supplier and payer,
VAT breakdown) so it files and reads like one. It still says plainly that it
was prepared here from the bank payment, not issued by TfL, and carries no
TfL branding: it is a record of the payment, never a copy of a TfL document.

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

# TfL's registered office, as on its own published documents
SUPPLIER_ADDRESS = ("5 Endeavour Square", "London", "E20 1JN")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif;
         color: #1a2433; font-size: 11.5px; line-height: 1.45; }
  .muted { color: #5b6878; }
  .label { font-size: 9px; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; color: #5b6878;
           margin: 0 0 5px 0; }
  .num { font-variant-numeric: tabular-nums; }
  .head { display: flex; justify-content: space-between; align-items: flex-start;
          padding-bottom: 16px; border-bottom: 3px solid #1a2433; }
  .company { font-size: 17px; font-weight: 700; }
  .title { font-size: 26px; font-weight: 800; letter-spacing: 0.14em; text-align: right; margin: -4px 0 8px 0; }
  .meta { border-collapse: collapse; margin-left: auto; }
  .meta td { padding: 1px 0 1px 16px; text-align: right; }
  .meta td:first-child { color: #5b6878; }
  .meta td:last-child { font-weight: 600; }
  .parties { display: flex; gap: 28px; margin: 22px 0; }
  .party { flex: 1; border: 1px solid #d5dbe2; border-radius: 4px; padding: 11px 14px; }
  .party b { font-size: 12.5px; }
  .items { width: 100%%; border-collapse: collapse; }
  .items th { background: #1a2433; color: #fff; font-size: 9px; letter-spacing: 0.08em; text-transform: uppercase;
              text-align: left; padding: 7px 9px; }
  .items td { padding: 10px 9px; border-bottom: 1px solid #d5dbe2; vertical-align: top; }
  .items .r { text-align: right; }
  .below { display: flex; justify-content: space-between; align-items: flex-start; margin-top: 16px; gap: 28px; }
  .stamp { margin-top: 6px; display: inline-block; border: 2.5px solid #1d7a46; color: #1d7a46; border-radius: 5px;
           padding: 3px 14px; font-size: 20px; font-weight: 800; letter-spacing: 0.2em; transform: rotate(-6deg); }
  .stamp small { display: block; font-size: 8.5px; letter-spacing: 0.06em; font-weight: 700; text-align: center; }
  .totals { border-collapse: collapse; min-width: 260px; }
  .totals td { padding: 5px 9px; text-align: right; }
  .totals td:first-child { text-align: left; color: #5b6878; }
  .totals tr.grand td { border-top: 2px solid #1a2433; border-bottom: 2px solid #1a2433; font-size: 14px;
                         font-weight: 800; color: #1a2433; padding: 8px 9px; }
  .details { margin-top: 26px; border-collapse: collapse; width: 100%%; }
  .details td { padding: 5px 0; border-bottom: 1px dotted #c3cbd4; }
  .details td:first-child { color: #5b6878; width: 34%%; }
  .basis { margin-top: 26px; padding: 11px 14px; background: #f3f5f8; border-left: 3px solid #1a2433;
           font-size: 10px; color: #3e4a59; }
  .basis p { margin: 0 0 5px 0; } .basis p:last-child { margin: 0; }
  .foot { margin-top: 22px; padding-top: 8px; border-top: 1px solid #d5dbe2; font-size: 9px; color: #5b6878;
          display: flex; justify-content: space-between; }
</style></head><body>
  <div class="head">
    <div><div class="company">%(company)s</div><div class="muted">Expense record · Travel</div></div>
    <div><div class="title">RECEIPT</div>
      <table class="meta">
        <tr><td>Receipt no.</td><td class="num">%(number)s</td></tr>
        <tr><td>Date of payment</td><td>%(paid_on)s</td></tr>
        <tr><td>Date issued</td><td>%(issued_on)s</td></tr>
      </table></div>
  </div>
  <div class="parties">
    <div class="party"><div class="label">Supplier</div><b>%(supplier)s</b><br>%(supplier_address)s</div>
    <div class="party"><div class="label">Paid by</div>%(payer)s</div>
  </div>
  <table class="items">
    <tr><th>Description</th><th>Date</th><th class="r">Qty</th><th class="r">Net</th><th class="r">VAT rate</th>
        <th class="r">VAT</th><th class="r">Amount</th></tr>
    <tr><td><b>Travel on TfL services</b><br><span class="muted">Contactless pay as you go fares</span></td>
        <td>%(paid_on_short)s</td><td class="r num">1</td><td class="r num">%(total)s</td>
        <td class="r">0%% (zero-rated)</td><td class="r num">%(zero)s</td><td class="r num">%(total)s</td></tr>
  </table>
  <div class="below">
    <div><div class="stamp">PAID<small>%(paid_on_short)s</small></div></div>
    <table class="totals">
      <tr><td>Net</td><td class="num">%(total)s</td></tr>
      <tr><td>VAT at 0%%</td><td class="num">%(zero)s</td></tr>
      <tr class="grand"><td>Total paid</td><td class="num">%(total)s</td></tr>
    </table>
  </div>
  <table class="details">%(details)s</table>
  <div class="basis"><div class="label">Basis of preparation</div>%(basis)s</div>
  <div class="foot"><span>Receipt no. %(number)s · Prepared by Receipt Bridge on %(issued_on)s</span><span>Page 1 of 1</span></div>
</body></html>"""


def is_tfl(description: str | None) -> bool:
    """A bank payment to TfL, by its statement line."""
    return bool(_TFL.search(description or ""))


def money(amount: float, currency: str = "GBP") -> str:
    symbol = _SYMBOLS.get(currency or "GBP")
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency}"


def short_date(day: str) -> str:
    try:
        return f"{date.fromisoformat(day[:10]):%d %b %Y}"
    except ValueError:
        return day


def long_date(day: str) -> str:
    try:
        d = date.fromisoformat(day[:10])
    except ValueError:
        return day
    return f"{d.day} {d:%B %Y}"


def receipt_number(payment: dict[str, Any]) -> str:
    """TFL-YYYYMMDD-<the bank transaction's id>: the same every time it's made."""
    tail = re.sub(r"[^A-Za-z0-9]", "", str(payment.get("url") or "").rstrip("/").rsplit("/", 1)[-1])[-12:]
    return f"TFL-{payment['dated_on'][:10].replace('-', '')}-{tail or '0'}"


def receipt_html(payment: dict[str, Any], account_name: str = "", company: str = "",
                 made_on: date | None = None) -> str:
    """The receipt page for one bank payment to TfL."""
    esc = html_lib.escape
    amount = abs(float(payment["amount"]))
    currency = payment.get("currency") or "GBP"
    statement_line = (payment.get("description") or "").split("//")[0].strip()
    paid_on = payment["dated_on"][:10]
    issued_on = long_date((made_on or date.today()).isoformat())
    number = receipt_number(payment)
    payer = "<br>".join([f"<b>{esc(company)}</b>"] * bool(company)
                        + [esc(account_name)] * bool(account_name) + ["Contactless card payment"])
    details = [
        ("Payment method", "Contactless card (pay as you go)"),
        ("Paid from", account_name),
        ("Bank statement reference", statement_line),
        ("Bank transaction", number.rsplit("-", 1)[-1] if payment.get("url") else ""),
        ("Amount debited", money(amount, currency)),
    ]
    basis = [
        "Transport for London does not issue receipts for contactless pay as you go fares. This receipt "
        f"records the payment as it appears on the bank statement{' of the account named above' * bool(account_name)}, "
        f"and was prepared by Receipt Bridge on {issued_on}. It is not issued by Transport for London.",
        "Fares for travel on TfL services are zero-rated for VAT. TfL normally charges a day's contactless "
        "journeys the following day; journey-level detail is available from a TfL account where the card is "
        "registered.",
    ]
    return PAGE % {
        "company": esc(company or "Expense record"),
        "number": esc(number),
        "paid_on": esc(long_date(paid_on)),
        "paid_on_short": esc(short_date(paid_on)),
        "issued_on": esc(issued_on),
        "supplier": esc(SUPPLIER),
        "supplier_address": "<br>".join(esc(line) for line in SUPPLIER_ADDRESS),
        "payer": payer,
        "total": esc(money(amount, currency)),
        "zero": esc(money(0, currency)),
        "details": "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in details if v),
        "basis": "".join(f"<p>{esc(text)}</p>" for text in basis),
    }


def render(pool: Any, payment: dict[str, Any], out_path: Path, account_name: str = "",
           pdf_config: dict[str, Any] | None = None, company: str = "") -> Path:
    """Print the receipt for `payment` to `out_path` (pool: a BrowserPool)."""
    from .pdf import html_to_pdf

    return html_to_pdf(pool, receipt_html(payment, account_name, company), out_path, pdf_config)
