"""Find an email receipt for a bank payment that has none (PLAN.md §11,
the Statement's "Use that email").

A payment of £23.99 to AMAZON* MKTPLACE with no receipt: search Gmail for
"23.99" in the days around the payment, and keep an email whose own text
shows that amount (as a total, ideally). The search is the only network
call and runs on the background worker; everything here is pure, so it is
tested with made-up emails.

Never guessed: the amount must appear in the email exactly, with a currency
mark, and not as part of a longer number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from email.utils import parseaddr
from typing import Any

from .supplier_builder import _amounts, _supplier_name

# A card payment usually settles 0–3 days after the order; the confirmation
# email can come a day after the order. Search a little wider either side.
DAYS_BEFORE = 7
DAYS_AFTER = 2
# Emails that are never receipts, even when they show the amount.
NOT_RECEIPTS = re.compile(
    r"statement|newsletter|your bill is ready|direct debit|payment (?:request|reminder)|"
    r"invoice (?:due|overdue)|refund|declined|failed",
    re.IGNORECASE,
)


@dataclass
class Found:
    message_id: str
    thread_id: str
    account: str
    sender: str
    supplier: str
    subject: str
    day: str           # the email's date, YYYY-MM-DD
    amount: float
    as_total: bool     # the amount sits next to "Total", "Amount paid"…

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def query(amount: float, paid_on: str) -> str:
    """Gmail search for one payment: the amount, in the days around it."""
    day = date.fromisoformat(paid_on[:10])
    after = day - timedelta(days=DAYS_BEFORE)
    before = day + timedelta(days=DAYS_AFTER + 1)       # `before:` is exclusive
    return (f'"{abs(amount):.2f}" after:{after:%Y/%m/%d} before:{before:%Y/%m/%d} '
            "-in:chats -in:spam -in:trash")


def _number(value: str) -> float | None:
    cleaned = value.replace(",", "").replace(" ", "")
    if cleaned.count(".") > 1:
        return None
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def amount_in(text: str, amount: float, currency: str = "GBP") -> tuple[bool, bool]:
    """(shown, as a total): does the text show this amount in this currency,
    and is it labelled like a total? £23.99 / 23.99 GBP count; 123.99,
    23.995, $23.99 and a bare 23.99 don't."""
    target = round(abs(amount), 2)
    shown = total = False
    for candidate in _amounts(text):
        if candidate.currency_shown and candidate.currency == currency and _number(candidate.value) == target:
            shown = True
            total = total or candidate.score > 0
    return shown, total


def judge(message: Any, amount: float, account: str, currency: str = "GBP") -> Found | None:
    """An email (app.email_message.Email) that shows the payment's amount,
    or None. The subject is checked for emails that are never receipts."""
    if NOT_RECEIPTS.search(message.subject or ""):
        return None
    text = f"{message.subject}\n{message.text}\n{message.attachment_text}"
    shown, total = amount_in(text, amount, currency)
    if not shown:
        return None
    display, address = parseaddr(message.sender or "")
    domain = address.rpartition("@")[2].lower()
    return Found(message_id=message.message_id, thread_id=message.thread_id, account=account,
                 sender=address, supplier=_supplier_name(display, domain), subject=message.subject or "",
                 day=message.date_iso, amount=round(abs(amount), 2), as_total=total)


def best(found: list[Found], paid_on: str) -> Found | None:
    """Prefer an amount labelled as a total, then the email nearest the
    payment date."""
    if not found:
        return None
    day = date.fromisoformat(paid_on[:10])

    def distance(f: Found) -> int:
        try:
            return abs((date.fromisoformat(f.day) - day).days)
        except ValueError:
            return 99

    return min(found, key=lambda f: (not f.as_total, distance(f)))
