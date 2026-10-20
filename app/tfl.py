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

# Receipt Bridge's icon (build_app.py, ICON_HTML) as a vector, sharp at any size
LOGO = """<svg class="logo" viewBox="0 0 1024 1024" width="%(size)d" height="%(size)d" aria-label="Receipt Bridge">
  <defs><linearGradient id="rb-bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#1d4ed8"/><stop offset="1" stop-color="#0f2f8f"/></linearGradient></defs>
  <rect width="1024" height="1024" rx="230" fill="url(#rb-bg)"/>
  <rect x="212" y="152" width="600" height="720" rx="48" fill="#fff"/>
  <rect x="278" y="222" width="468" height="34" rx="17" fill="#c9d3e4"/>
  <rect x="278" y="290" width="384" height="34" rx="17" fill="#c9d3e4"/>
  <rect x="278" y="358" width="271" height="34" rx="17" fill="#c9d3e4"/>
  <rect x="278" y="448" width="468" height="8" rx="4" fill="#1f2933"/>
  <text x="278" y="572" font-family="-apple-system, Helvetica, Arial" font-size="74" font-weight="800" fill="#1f2933">£</text>
  <text x="746" y="572" text-anchor="end" font-family="-apple-system, Helvetica, Arial" font-size="74" font-weight="800" fill="#1f2933">✓</text>
</svg>"""

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif;
         color: #1a2433; font-size: 11.5px; line-height: 1.45; }
  .muted { color: #5b6878; }
  .num { font-variant-numeric: tabular-nums; }
  .label { font-size: 9px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: #5b6878; }
  .brand { display: flex; justify-content: space-between; align-items: center; padding-bottom: 12px;
           border-bottom: 1px solid #d5dbe2; }
  .brand .who { display: flex; align-items: center; gap: 10px; }
  .brand .name { font-size: 13px; font-weight: 700; }
  .brand .meta { text-align: right; font-size: 10.5px; color: #5b6878; }
  .brand .meta b { color: #1a2433; font-weight: 600; }
  .title { margin: 22px 0 2px 0; font-size: 26px; font-weight: 800; letter-spacing: -0.01em; }
  .when { font-size: 13px; color: #3e4a59; }
  .trip { display: flex; gap: 0; margin-top: 18px; border: 1px solid #d5dbe2; border-radius: 10px; overflow: hidden; }
  .route { flex: 1.25; padding: 20px 22px 18px; }
  .details { flex: 1; padding: 20px 22px 18px; background: #f5f7fa; border-left: 1px solid #d5dbe2; }
  .stops { margin-top: 10px; }
  .stop { display: flex; gap: 14px; }
  .rail { width: 18px; display: flex; flex-direction: column; align-items: center; flex: none; }
  .dot { width: 18px; height: 18px; border-radius: 50%%; border: 4px solid #1d4ed8; background: #fff; flex: none; }
  .dot.end { background: #1d4ed8; }
  .dot.back { border-color: #9fb3e6; }
  .seg { width: 4px; flex: 1; min-height: 40px; background: #1d4ed8; margin: 2px 0; }
  .seg.back { background: repeating-linear-gradient(#9fb3e6 0 6px, transparent 6px 11px); }
  .stop .text { padding-bottom: 14px; }
  .stop .what { font-size: 9px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: #5b6878; }
  .stop .place { font-size: 16px; font-weight: 700; line-height: 1.25; }
  .stop .note { color: #5b6878; margin-top: 2px; }
  .stop.faint .place { color: #5b6878; font-weight: 600; font-size: 13px; }
  .unrecorded .place { font-size: 15px; font-weight: 700; }
  dl { margin: 10px 0 0 0; }
  dt { font-size: 9px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: #5b6878; margin-top: 10px; }
  dt:first-child { margin-top: 0; }
  dd { margin: 2px 0 0 0; font-size: 12.5px; }
  .cost { display: flex; margin-top: 18px; border: 1px solid #d5dbe2; border-radius: 10px; }
  .cost > div { flex: 1; padding: 12px 16px; border-left: 1px solid #d5dbe2; }
  .cost > div:first-child { border-left: 0; }
  .cost .v { font-size: 15px; font-weight: 700; margin-top: 3px; }
  .cost .s { color: #5b6878; font-size: 10.5px; }
  .evidence { margin-top: 18px; }
  .evidence table { width: 100%%; border-collapse: collapse; margin-top: 6px; }
  .evidence td { padding: 5px 0; border-bottom: 1px dotted #c3cbd4; }
  .evidence td:first-child { color: #5b6878; width: 30%%; }
  .about { margin-top: 20px; padding: 11px 14px; background: #f5f7fa; border-radius: 8px; font-size: 10px; color: #3e4a59; }
  .about p { margin: 4px 0 0 0; }
  .foot { margin-top: 18px; padding-top: 8px; border-top: 1px solid #d5dbe2; font-size: 9px; color: #5b6878;
          display: flex; justify-content: space-between; align-items: center; }
  .foot .who { display: flex; align-items: center; gap: 6px; }
</style></head><body>
  <div class="brand">
    <div class="who">%(logo)s<div><div class="name">Receipt Bridge</div><div class="muted">Travel expense records</div></div></div>
    <div class="meta">Record <b class="num">%(number)s</b><br>Prepared %(issued_on)s</div>
  </div>
  <div class="title">Travel record</div>
  <div class="when">%(when)s</div>
  <div class="trip">
    <div class="route"><div class="label">Journey</div>%(journey)s</div>
    <div class="details"><div class="label">Trip</div><dl>%(facts)s</dl></div>
  </div>
  <div class="cost">
    <div><div class="label">Fare</div><div class="v num">%(total)s</div><div class="s">Contactless pay as you go</div></div>
    <div><div class="label">VAT</div><div class="v num">%(zero)s</div><div class="s">0%% (zero-rated)</div></div>
    <div><div class="label">Charged</div><div class="v">%(paid_on_short)s</div><div class="s">%(account)s</div></div>
  </div>
  <div class="evidence"><div class="label">Payment evidence</div><table>%(payment)s</table></div>
  <div class="about"><div class="label">About this record</div>%(basis)s</div>
  <div class="foot"><span class="who">%(small_logo)s Prepared with Receipt Bridge · Record %(number)s</span><span>Page 1 of 1</span></div>
</body></html>"""

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


def weekday_date(day: str) -> str:
    """Monday 19 October 2026."""
    try:
        d = date.fromisoformat(day[:10])
    except ValueError:
        return day
    return f"{d:%A} {d.day} {d:%B %Y}"


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
    date (YYYY-MM-DD), reason and return (True: there and back), each only
    when given."""
    fields = fields or {}
    out: dict[str, Any] = {key: place(fields.get(key), limit) for key, limit in FIELD_LENGTH.items()}
    try:
        out["date"] = date.fromisoformat(str(fields.get("date") or "")[:10]).isoformat()
    except ValueError:
        out["date"] = ""
    out["return"] = fields.get("return") is True        # there and back
    return {key: value for key, value in out.items() if value}


def describe(trip: dict[str, str] | None) -> str:
    """The receipt's description: the journey, when there is one."""
    trip = trip or {}
    if trip.get("from") and trip.get("to"):
        return f"TfL travel: {trip['from']} to {trip['to']}{' and back' if trip.get('return') else ''}"
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
    you entered (`trip`: from, to, date, reason, return; any of them)."""
    esc = html_lib.escape
    trip = journey(trip)
    amount = abs(float(payment["amount"]))
    currency = payment.get("currency") or "GBP"
    statement_line = (payment.get("description") or "").split("//")[0].strip()
    paid_on = payment["dated_on"][:10]
    issued_on = long_date((made_on or date.today()).isoformat())
    number = receipt_number(payment)
    travelled = trip.get("date")
    when = " · ".join(filter(None, [company, weekday_date(travelled) if travelled else f"Charged {long_date(paid_on)}",
                                    "Transport for London"]))

    def stop(kind: str, what: str, place: str, note: str = "", last: bool = False, cls: str = "") -> str:
        rail = f'<span class="dot {kind}"></span>' + ("" if last else f'<span class="seg {cls}"></span>')
        return (f'<div class="stop {cls}"><div class="rail">{rail}</div><div class="text"><div class="what">{esc(what)}</div>'
                f'<div class="place">{esc(place)}</div>{f"<div class=note>{esc(note)}</div>" if note else ""}</div></div>')

    if trip.get("from") or trip.get("to"):
        start, end = trip.get("from") or "Not recorded", trip.get("to") or "Not recorded"
        stops = stop("start", "From", start, "TfL · contactless pay as you go")
        if trip.get("return"):
            stops += stop("end", "To", end, "", False, "back")
            stops += stop("back", "Return", f"Back to {start}", "", True, "faint")
        else:
            stops += stop("end", "To", end, "", True)
        route = f'<div class="stops">{stops}</div>'
    else:
        route = ('<div class="stops unrecorded"><div class="place">Travel on TfL services</div>'
                 '<div class="muted">Contactless pay as you go · journeys not recorded</div></div>')
    facts = [
        ("Date of travel", long_date(travelled) if travelled else ""),
        ("Journey type", ("Return" if trip.get("return") else "Single") if trip.get("from") or trip.get("to")
         else "Return" if trip.get("return") else ""),
        ("Purpose", trip.get("reason", "")),
        ("Network", "Transport for London"),
        ("Traveller", company),
    ]
    payment_rows = [
        ("Paid from", f"{account_name} (contactless card)" if account_name else "Contactless card"),
        ("Bank statement shows", statement_line),
        ("Bank transaction", number.rsplit("-", 1)[-1] if payment.get("url") else ""),
        ("Amount debited", f"{money(amount, currency)} on {long_date(paid_on)}"),
        ("Supplier", f"{SUPPLIER}, {', '.join(SUPPLIER_ADDRESS)}"),
    ]
    basis = [
        f"This record of travel shows the payment as it appears on the bank statement{' of the account named above' * bool(account_name)}, "
        f"and was prepared by Receipt Bridge on {issued_on}. It is not issued by Transport for London.",
        "Fares for travel on TfL services are zero-rated for VAT. TfL normally charges a day's contactless "
        "journeys together, the following day, so one charge can cover several journeys.",
    ]
    if trip:
        basis.append("The journey details above were entered by the traveller and are not confirmed by "
                     "Transport for London.")
    return PAGE % {
        "logo": LOGO % {"size": 34},
        "small_logo": LOGO.replace('id="rb-bg"', 'id="rb-bg2"').replace("url(#rb-bg)", "url(#rb-bg2)") % {"size": 12},
        "number": esc(number),
        "issued_on": esc(issued_on),
        "when": esc(when),
        "journey": route,
        "facts": "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in facts if v),
        "payment": "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in payment_rows if v),
        "total": esc(money(amount, currency)),
        "zero": esc(money(0, currency)),
        "paid_on_short": esc(short_date(paid_on)),
        "account": esc(account_name or "Contactless card"),
        "basis": "".join(f"<p>{esc(text)}</p>" for text in basis),
    }


def render(pool: Any, payment: dict[str, Any], out_path: Path, account_name: str = "",
           pdf_config: dict[str, Any] | None = None, company: str = "",
           journey: dict[str, str] | None = None) -> Path:
    """Print the travel record for `payment` to `out_path` (pool: a BrowserPool)."""
    from .pdf import html_to_pdf

    return html_to_pdf(pool, receipt_html(payment, account_name, company, journey), out_path, pdf_config)
