"""Receipts for TfL contactless travel, made from the bank payment.

TfL sends nothing for pay-as-you-go travel on a contactless card: no email,
no receipt, and its journey history only exists for a card registered with a
TfL account. The bank payment is the only record there is, so Receipt Bridge
prints one from it, laid out as a travel record: the journey you entered
(start, end, date, reason; all optional), the fare and its VAT, and the
payment. It still says plainly that it
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

# TfL's head office
SUPPLIER_ADDRESS = ("5 Endeavour Square", "London", "E20 1JN")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif;
         color: #1a2433; font-size: 11.5px; line-height: 1.45; }
  .muted { color: #5b6878; }
  .num { font-variant-numeric: tabular-nums; }
  .label { font-size: 9px; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; color: #5b6878;
           margin: 0 0 6px 0; }
  .head { display: flex; justify-content: space-between; align-items: flex-start;
          padding-bottom: 14px; border-bottom: 3px solid #1a2433; }
  .company { font-size: 17px; font-weight: 700; }
  .title { font-size: 22px; font-weight: 800; letter-spacing: 0.12em; text-align: right; margin: -3px 0 8px 0; }
  .meta { border-collapse: collapse; margin-left: auto; }
  .meta td { padding: 1px 0 1px 16px; text-align: right; }
  .meta td:first-child { color: #5b6878; }
  .meta td:last-child { font-weight: 600; }
  .journey { margin: 22px 0 0 0; border: 1px solid #d5dbe2; border-radius: 8px; padding: 18px 22px 16px; }
  .route { display: flex; align-items: flex-start; margin-top: 4px; }
  .stop { width: 34%%; }
  .stop.end { text-align: right; }
  .stop .name { font-size: 17px; font-weight: 700; line-height: 1.25; }
  .stop .what { font-size: 9px; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; color: #5b6878; }
  .line { flex: 1; position: relative; height: 26px; margin: 0 6px; }
  .line::before { content: ""; position: absolute; left: 7px; right: 7px; top: 12px; border-top: 3px solid #1a2433; }
  .dot { position: absolute; top: 6px; width: 15px; height: 15px; border-radius: 50%%; background: #fff;
         border: 3.5px solid #1a2433; }
  .dot.a { left: 0; } .dot.b { right: 0; }
  .network { font-size: 15px; font-weight: 700; }
  .grid { display: flex; gap: 22px; margin-top: 18px; }
  .facts { flex: 1; width: 100%%; border-collapse: collapse; }
  .facts td { padding: 6px 0; border-bottom: 1px dotted #c3cbd4; vertical-align: top; }
  .facts td:first-child { color: #5b6878; width: 38%%; padding-right: 12px; }
  .facts.pay td:first-child { width: 27%%; }
  .fare { width: 210px; border: 2px solid #1a2433; border-radius: 8px; padding: 12px 14px; align-self: flex-start; }
  .fare .amount { font-size: 26px; font-weight: 800; margin: 2px 0 8px 0; }
  .fare table { width: 100%%; border-collapse: collapse; }
  .fare td { padding: 3px 0; border-top: 1px solid #d5dbe2; }
  .fare td:last-child { text-align: right; }
  h2 { font-size: 9px; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; color: #5b6878;
       margin: 24px 0 4px 0; padding-bottom: 5px; border-bottom: 1.5px solid #1a2433; }
  .basis { margin-top: 24px; padding: 11px 14px; background: #f3f5f8; border-left: 3px solid #1a2433;
           font-size: 10px; color: #3e4a59; }
  .basis p { margin: 0 0 5px 0; } .basis p:last-child { margin: 0; }
  .foot { margin-top: 20px; padding-top: 8px; border-top: 1px solid #d5dbe2; font-size: 9px; color: #5b6878;
          display: flex; justify-content: space-between; }
</style></head><body>
  <div class="head">
    <div><div class="company">%(company)s</div><div class="muted">Travel expense record</div></div>
    <div><div class="title">TRAVEL RECORD</div>
      <table class="meta">
        <tr><td>Record no.</td><td class="num">%(number)s</td></tr>
        <tr><td>Date charged</td><td>%(paid_on)s</td></tr>
        <tr><td>Date issued</td><td>%(issued_on)s</td></tr>
      </table></div>
  </div>
  <div class="journey"><div class="label">Journey</div>%(journey)s</div>
  <div class="grid">
    <table class="facts">%(facts)s</table>
    <div class="fare"><div class="label">Fare paid</div><div class="amount num">%(total)s</div>
      <table><tr><td>Net</td><td class="num">%(total)s</td></tr>
        <tr><td>VAT 0%% (zero-rated)</td><td class="num">%(zero)s</td></tr></table></div>
  </div>
  <h2>Payment</h2>
  <table class="facts pay">%(payment)s</table>
  <div class="basis"><div class="label">Basis of preparation</div>%(basis)s</div>
  <div class="foot"><span>Record no. %(number)s · Prepared by Receipt Bridge on %(issued_on)s</span><span>Page 1 of 1</span></div>
</body></html>"""

ROUTE = """<div class="route">
  <div class="stop"><div class="what">Journey start</div><div class="name">%(start)s</div></div>
  <div class="line"><span class="dot a"></span><span class="dot b"></span></div>
  <div class="stop end"><div class="what">Journey end</div><div class="name">%(end)s</div></div>
</div>"""

FIELD_LENGTH = {"from": 80, "to": 80, "reason": 200}

_STATIONS = Path(__file__).with_name("tfl_stations.txt")


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


def place(text: Any, limit: int = 80) -> str:
    """One line of what you typed: spaces tidied, at most `limit` long."""
    return " ".join(str(text or "").split())[:limit]


def journey(fields: dict[str, Any] | None) -> dict[str, str]:
    """Where and why you travelled, as kept on the receipt: from, to,
    date (YYYY-MM-DD) and reason, each only when given."""
    fields = fields or {}
    out = {key: place(fields.get(key), limit) for key, limit in FIELD_LENGTH.items()}
    try:
        out["date"] = date.fromisoformat(str(fields.get("date") or "")[:10]).isoformat()
    except ValueError:
        out["date"] = ""
    return {key: value for key, value in out.items() if value}


def describe(trip: dict[str, str] | None) -> str:
    """The receipt's description: the journey, when there is one."""
    trip = trip or {}
    if trip.get("from") and trip.get("to"):
        return f"TfL travel: {trip['from']} to {trip['to']}"
    if trip.get("from") or trip.get("to"):
        return f"TfL travel: {'from ' + trip['from'] if trip.get('from') else 'to ' + trip['to']}"
    return "Contactless travel"


def stations() -> list[str]:
    """TfL's stations, for the journey start / end suggestions."""
    try:
        lines = _STATIONS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line for line in lines if line and not line.startswith("#")]


def receipt_html(payment: dict[str, Any], account_name: str = "", company: str = "",
                 trip: dict[str, str] | None = None, made_on: date | None = None) -> str:
    """The travel record for one bank payment to TfL, with the journey
    you entered (`trip`: from, to, date, reason; any of them)."""
    esc = html_lib.escape
    trip = journey(trip)
    amount = abs(float(payment["amount"]))
    currency = payment.get("currency") or "GBP"
    statement_line = (payment.get("description") or "").split("//")[0].strip()
    paid_on = payment["dated_on"][:10]
    issued_on = long_date((made_on or date.today()).isoformat())
    number = receipt_number(payment)
    if trip.get("from") or trip.get("to"):
        route = ROUTE % {"start": esc(trip.get("from") or "Not recorded"), "end": esc(trip.get("to") or "Not recorded")}
    else:
        route = ('<div class="network">Travel on TfL services</div>'
                 '<div class="muted">Contactless pay as you go · journeys not recorded</div>')
    facts = [
        ("Date of travel", long_date(trip["date"]) if trip.get("date") else ""),
        ("Reason for travel", trip.get("reason", "")),
        ("Network", "Transport for London, contactless pay as you go"),
        ("Supplier", f"{SUPPLIER}, {', '.join(SUPPLIER_ADDRESS)}"),
    ]
    payment_rows = [
        ("Paid by", company),
        ("Paid from", account_name),
        ("Payment method", "Contactless card"),
        ("Bank statement reference", statement_line),
        ("Bank transaction", number.rsplit("-", 1)[-1] if payment.get("url") else ""),
        ("Amount debited", money(amount, currency)),
    ]
    basis = [
        "Transport for London does not issue receipts for contactless pay as you go fares. This record "
        f"shows the payment as it appears on the bank statement{' of the account named above' * bool(account_name)}, "
        f"and was prepared by Receipt Bridge on {issued_on}. It is not issued by Transport for London.",
        "Fares for travel on TfL services are zero-rated for VAT. TfL normally charges a day's contactless "
        "journeys together, the following day, so one charge can cover several journeys.",
    ]
    if trip:
        basis.append("The journey details above were entered by the traveller and are not confirmed by "
                     "Transport for London.")
    rows = lambda pairs: "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in pairs if v)  # noqa: E731
    return PAGE % {
        "company": esc(company or "Travel record"),
        "number": esc(number),
        "paid_on": esc(long_date(paid_on)),
        "issued_on": esc(issued_on),
        "journey": route,
        "facts": rows(facts),
        "payment": rows(payment_rows),
        "total": esc(money(amount, currency)),
        "zero": esc(money(0, currency)),
        "basis": "".join(f"<p>{esc(text)}</p>" for text in basis),
    }


def render(pool: Any, payment: dict[str, Any], out_path: Path, account_name: str = "",
           pdf_config: dict[str, Any] | None = None, company: str = "",
           journey: dict[str, str] | None = None) -> Path:
    """Print the travel record for `payment` to `out_path` (pool: a BrowserPool)."""
    from .pdf import html_to_pdf

    return html_to_pdf(pool, receipt_html(payment, account_name, company, journey), out_path, pdf_config)
