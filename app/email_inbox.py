"""The Emails view: browse a mailbox and turn any one email into a receipt.

Supplier rules collect the receipts that come every month; this is for the
one-offs — a hotel, a conference ticket, a shop used once — where writing a
rule would be overkill. The list shows every email, and the ones that look
like receipts are picked out so they're easy to spot.

Everything here is pure: the list is judged from what Gmail's cheap
metadata request returns (subject, sender, snippet, labels), never the
whole email, so a page of fifty costs one round trip. Only the email you
open is downloaded in full, and only then is it read for a total.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any

from .supplier_builder import _amounts, _ranked, _supplier_name, _vat_label

# What a page of the list leaves out: chats, drafts, what you sent, and the
# bins. "All mail" means everything else, archived mail included.
BASE_QUERY = "-in:chats -in:drafts -in:sent -in:spam -in:trash"
# "Likely receipts": the words a receipt's subject or body nearly always has.
# Gmail does the searching, so this finds them across the whole mailbox, not
# just the page already loaded. Judged again below, like any other email.
RECEIPT_TERMS = ('{receipt invoice "order confirmation" "your order" "booking confirmation" '
                 '"payment confirmation" "payment received" "thank you for your purchase" '
                 '"thanks for your order" "amount paid" "tax invoice" "e-ticket"}')

# A subject that says what it is: "Your receipt from Apple", "Invoice INV-0042",
# "Order confirmation #123", "Your Uber trip".
SUBJECT_STRONG = re.compile(
    r"\breceipts?\b|\binvoice\b|\border\s+(?:confirm|#|no\b|number|\d)|"
    r"\b(?:payment|purchase|booking|reservation|order)\s+(?:confirm|received|successful|complete)|"
    r"\bconfirm(?:ation|ed)?\b.{0,20}\b(?:order|booking|payment|purchase|reservation)\b|"
    r"thank(?:s| you) for (?:your )?(?:order|purchase|payment|booking|shopping)|"
    r"\byour (?:\w+ )?(?:order|booking|purchase|trip|ride)\b|\byou(?:'ve)? paid\b|\be-?tickets?\b|"
    r"\bsubscription (?:renewed|confirmation)|\brenewal confirm",
    re.IGNORECASE,
)
# Words a receipt's body (the snippet) tends to have.
BODY_WORDS = re.compile(
    r"\b(?:total|amount paid|amount charged|order (?:number|no|#)|invoice (?:number|no|#)|"
    r"receipt (?:number|no|#)|vat|subtotal|payment method|paid with|billed to|charged)\b",
    re.IGNORECASE,
)
# Who it's from: a mailbox that only ever sends money mail.
SENDER_STRONG = re.compile(r"^(?:receipts?|invoices?|billing|payments?|accounts?)[._-]?", re.IGNORECASE)
SENDER_NAME = re.compile(r"\b(?:receipts?|invoices?|invoicing|billing)\b", re.IGNORECASE)   # "Uber Receipts"
SENDER_WEAK = re.compile(r"^(?:orders?|bookings?|purchases?|store|shop|sales)[._-]?", re.IGNORECASE)
# Never receipts, even when they mention money: the same list the Statement's
# email search uses, plus the sales and newsletters that show prices.
NOT_RECEIPT = re.compile(
    r"\bstatement\b|newsletter|your bill is ready|direct debit|payment (?:request|reminder)|"
    r"invoice (?:due|overdue)|\brefund|declined|\bfailed\b|"
    r"\d+\s*% off|\bsale\b|\bdeals?\b|\boffers?\b|discount|webinar|last chance|don't miss|"
    r"\bpassword\b|verify your|sign[- ]in|security alert|\bcode\b",
    re.IGNORECASE,
)
# Gmail's own tabs: Promotions and Social are almost never receipts.
LABEL_PENALTY = {"CATEGORY_PROMOTIONS": 3, "CATEGORY_SOCIAL": 3, "CATEGORY_FORUMS": 2}

LIKELY = "likely"
MAYBE = "maybe"
LIKELY_AT = 4
MAYBE_AT = 2


@dataclass
class Signal:
    """How much an email looks like a receipt, and why, in a few words."""

    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def level(self) -> str | None:
        if self.score >= LIKELY_AT:
            return LIKELY
        if self.score >= MAYBE_AT:
            return MAYBE
        return None


def list_query(search: str = "", receipts_only: bool = False) -> str:
    """The Gmail search for one page of the list. `search` is passed through,
    so Gmail's own operators (from:, after:, has:attachment) work too."""
    parts = [search.strip()] if search.strip() else []
    if receipts_only:
        parts.append(RECEIPT_TERMS)
    parts.append(BASE_QUERY)
    return " ".join(parts)


def clean_snippet(snippet: str) -> str:
    """Gmail's snippet arrives HTML-escaped ("Thanks &amp; welcome")."""
    return " ".join(html_lib.unescape(snippet or "").split())


def headline_amount(text: str) -> dict[str, Any] | None:
    """The amount a list line shows: a figure with its currency, a labelled
    total ahead of any other. None when the text shows no money."""
    shown = [c for c in _ranked(_amounts(text)) if c.currency_shown]
    if not shown:
        return None
    best = shown[0]
    try:
        value = float(best.value.replace(",", ""))
    except ValueError:
        return None
    return {"amount": round(value, 2), "currency": best.currency or "GBP", "as_total": best.score > 0}


def judge(subject: str, sender: str, snippet: str, labels: list[str] | tuple[str, ...] = (),
          has_attachment: bool = False) -> Signal:
    """Score one email from its list metadata alone."""
    signal = Signal()
    subject = subject or ""
    snippet = clean_snippet(snippet)

    def add(points: int, why: str) -> None:
        signal.score += points
        if why and points > 0:
            signal.reasons.append(why)

    if SUBJECT_STRONG.search(subject):
        add(3, "The subject says it's a receipt or order")
    if NOT_RECEIPT.search(subject):
        add(-4, "")
    money = headline_amount(f"{subject}\n{snippet}")
    if money:
        add(2 if money["as_total"] else 1, "Shows a total" if money["as_total"] else "Shows an amount")
    if BODY_WORDS.search(snippet):
        add(1, "Mentions an order, invoice or VAT")
    display, address = parseaddr(sender or "")
    local = address.partition("@")[0]
    if SENDER_STRONG.match(local) or SENDER_NAME.search(display):
        add(2, "Sent from a billing address")
    elif SENDER_WEAK.match(local):
        add(1, "Sent from an orders address")
    if has_attachment and signal.score > 0:
        add(1, "Has an attachment")
    for label in labels or ():
        signal.score -= LABEL_PENALTY.get(label, 0)
    return signal


def list_row(header: dict[str, Any], account: str) -> dict[str, Any]:
    """One line of the list, from `GmailClient.headers`."""
    snippet = clean_snippet(header.get("snippet", ""))
    display, address = parseaddr(header.get("sender", ""))
    signal = judge(header.get("subject", ""), header.get("sender", ""), snippet,
                   header.get("label_ids") or [], bool(header.get("has_attachment")))
    return {
        "id": header["id"],
        "thread_id": header.get("thread_id", ""),
        "account": account,
        "subject": header.get("subject", ""),
        "from_name": display or address,
        "from_address": address,
        "date": header.get("internal_date") or header.get("date", ""),
        "snippet": snippet,
        "has_attachment": bool(header.get("has_attachment")),
        "unread": "UNREAD" in (header.get("label_ids") or []),
        "amount": headline_amount(f"{header.get('subject', '')}\n{snippet}"),
        "receipt": signal.level,
        "why": signal.reasons,
    }


def draft(message: Any) -> dict[str, Any]:
    """What the email would become: supplier, date, total, VAT, read from the
    whole email and any PDF it carries. The user can change any of it before
    it's added; anything not found is left blank, never guessed."""
    display, address = parseaddr(message.sender or "")
    domain = address.rpartition("@")[2].lower()
    pdfs = [a for a in message.pdf_attachments() if a.get("data")]
    candidates = _amounts(message.text)
    if pdfs:
        candidates += _amounts(message.attachment_text, "attachment_text")
    ranked = [c for c in _ranked(candidates) if c.currency]
    best = ranked[0] if ranked else None
    total = float(best.value.replace(",", "")) if best else None
    currency = (best.currency if best else "") or "GBP"
    vats = sorted((c for c in candidates if _vat_label(c.label) and c.currency == currency),
                  key=lambda c: -float(c.value.replace(",", "")))
    vat = float(vats[0].value.replace(",", "")) if vats else None
    if vat is not None and total is not None and vat >= total:
        vat = None
    return {
        "supplier": _supplier_name(display, domain) if (display or domain) else "",
        "date": message.date_iso,
        "total": round(total, 2) if total is not None else None,
        "currency": currency,
        "vat": round(vat, 2) if vat is not None else None,
        "total_label": best.label if best else "",
        "total_found_in": ("the attached PDF" if best and best.source == "attachment_text" else "the email") if best else "",
        "amounts": [{"amount": float(c.value.replace(",", "")), "currency": c.currency, "label": c.label}
                    for c in ranked[:6]],
        "pdf": pdfs[0]["filename"] if pdfs else "",
    }
