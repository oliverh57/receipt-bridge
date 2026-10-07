"""Pair receipts with FreeAgent bank transactions (PLAN.md §8, §11).

Read-only: this proposes, it never files. Statuses:

  matched          a payment is chosen. Exact amount in the date window; when
                   several identical payments fit (the same fare most days)
                   the nearest date is taken, never asked. Or a payment you
                   pinned yourself ("This one", "Use this", Change payment).
  choose           the receipt has no date of its own (it came from the
                   photo) and several payments fit: you pick the day ("Which day
                   is this?").
  waiting          nothing fits yet. `options` lists near misses: payments
                   from the same supplier for a little more (a tip).
  expense_but_found  marked as paid personally, yet an exact business
                   payment exists. A wrong tap on the phone? (§6.3)
  not_applicable   expense, no total or date, or a currency the bank
                   accounts aren't in.

Amounts compare in pence. A card payment is a negative transaction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

# Card payments usually settle 0–3 days after the receipt date; a week
# covers weekends and slow feeds. A transaction dated before the receipt is
# rare (pre-authorisations), so only two days are allowed that way.
DAYS_BEFORE = 2
DAYS_AFTER = 7
# A receipt without its own date (the photo's date stands in) may have been
# photographed weeks later.
UNDATED_DAYS_BEFORE = 60
# Near misses: the same supplier, charged up to this much more (tips, service).
NEAR_MISS_SHARE = 0.30


@dataclass
class Match:
    status: str
    transaction: dict[str, Any] | None = None
    candidates: int = 0
    reason: str = ""
    alias_hit: bool = False
    days: int | None = None                 # payment date minus receipt date
    pinned: bool = False
    exact: bool = True                      # the payment is exactly the receipt total
    options: list[dict[str, Any]] = field(default_factory=list)   # choose / near misses


@dataclass
class _Receipt:
    id: int
    pence: int
    when: date
    paid_by: str | None
    token: str
    undated: bool
    pinned: str | None
    account: str | None = None              # only this bank account's payments
    candidates: list[dict] = field(default_factory=list)
    refused: frozenset[str] = frozenset()   # payments you said it isn't ("Remove file")


def _pence(value: float) -> int:
    return int(round(float(value) * 100))


def supplier_token(name: str | None) -> str:
    """The word a bank statement is most likely to show: the first word of
    three or more letters, uppercased ("Pret A Manger" → PRET)."""
    for word in re.findall(r"[A-Za-z]{3,}", name or ""):
        return word.upper()
    return ""


def needs_receipt(t: dict[str, Any]) -> bool:
    """A payment out that's wholly unexplained, or explained already (a bank
    rule, an accepted guess) by one unlocked explanation with no receipt."""
    if float(t["amount"]) >= 0:
        return False
    if t.get("unexplained_amount") not in (None, 0, 0.0):
        return float(t["unexplained_amount"]) == float(t["amount"])
    return bool(t.get("explanation_url")) and not t.get("explanation_attachments") \
        and not t.get("explanation_locked")


def _days(t: dict[str, Any], when: date) -> int:
    return (date.fromisoformat(t["dated_on"]) - when).days


def _named(t: dict[str, Any], token: str) -> bool:
    return bool(token and token in (t.get("description") or "").upper())


def match_receipts(receipts: list[dict[str, Any]], transactions: list[dict[str, Any]],
                   account_currencies: set[str]) -> dict[int, Match]:
    """`receipts`: dicts with id, total, currency, date (ISO), paid_by, vendor,
    and optionally `pinned` (a transaction URL you chose), `undated` (the
    date is the photo's, not the receipt's) and `refused` (payments you said
    it isn't). `transactions`: dicts with url,
    dated_on, amount, unexplained_amount, description, currency, and the
    explanation_* fields from the cache."""
    results: dict[int, Match] = {}
    open_tx = [t for t in transactions if needs_receipt(t)]
    by_url = {t["url"]: t for t in open_tx}

    eligible: list[_Receipt] = []
    for r in receipts:
        if r.get("total") is None or not r.get("date"):
            results[r["id"]] = Match("not_applicable", reason="needs a total and a date")
            continue
        if (r.get("currency") or "") not in account_currencies:
            results[r["id"]] = Match("not_applicable", reason="not in the bank account's currency")
            continue
        eligible.append(_Receipt(r["id"], _pence(r["total"]), date.fromisoformat(r["date"][:10]),
                                 r.get("paid_by"), supplier_token(r.get("vendor")),
                                 bool(r.get("undated")), r.get("pinned"), r.get("account"),
                                 refused=frozenset(r.get("refused") or ())))

    for rec in eligible:
        before = UNDATED_DAYS_BEFORE if rec.undated else DAYS_BEFORE
        window = [t for t in open_tx
                  if (rec.account is None or t.get("bank_account") == rec.account)
                  and t["url"] not in rec.refused
                  and _pence(-float(t["amount"])) == rec.pence
                  and -before <= _days(t, rec.when) <= DAYS_AFTER]
        named = [t for t in window if _named(t, rec.token)]
        # Same amount from a different supplier is not a candidate when one
        # that names this supplier exists.
        rec.candidates = named or window

    # Expenses: no bank matching, but a wrong tap on the phone shows up as an
    # exact business payment (§6.3).
    for rec in [r for r in eligible if r.paid_by == "personal"]:
        results[rec.id] = (Match("expense_but_found", rec.candidates[0], len(rec.candidates),
                                 "an exact business payment exists: paid from the business account?")
                           if rec.candidates else Match("not_applicable", reason="expense"))

    business = [r for r in eligible if r.paid_by != "personal"]
    claimed: set[str] = set()

    # Payments you chose come first, whatever their amount.
    for rec in business:
        chosen = by_url.get(rec.pinned) if rec.pinned else None
        if chosen is None or chosen["url"] in claimed:
            continue
        claimed.add(chosen["url"])
        exact = _pence(-float(chosen["amount"])) == rec.pence
        results[rec.id] = Match("matched", chosen, len(rec.candidates), "you chose this payment",
                                _named(chosen, rec.token), _days(chosen, rec.when), True, exact)

    # Then, oldest receipt first, the nearest unclaimed payment that fits
    # (a payment on or after the receipt date wins a tie). Taking receipts in
    # date order pairs repeated identical amounts the way a card settles.
    for rec in sorted((r for r in business if r.id not in results), key=lambda r: (r.when, r.id)):
        free = [t for t in rec.candidates if t["url"] not in claimed]
        if rec.undated and len(free) > 1:
            options = sorted(free, key=lambda t: t["dated_on"])
            results[rec.id] = Match("choose", None, len(free), f"{len(free)} possible payments",
                                    options=options)
            continue
        if not free:
            near = _near_misses(rec, open_tx, claimed)
            reason = ("every matching payment is already paired with another receipt"
                      if rec.candidates else "no payment for this amount yet")
            results[rec.id] = Match("waiting", None, len(rec.candidates), reason, options=near)
            continue
        chosen = min(free, key=lambda t: (abs(_days(t, rec.when)), _days(t, rec.when) < 0, t["dated_on"]))
        claimed.add(chosen["url"])
        reason = "" if len(rec.candidates) == 1 else f"nearest of {len(rec.candidates)} identical payments"
        results[rec.id] = Match("matched", chosen, len(rec.candidates), reason,
                                _named(chosen, rec.token), _days(chosen, rec.when))
    return results


def _near_misses(rec: _Receipt, open_tx: list[dict[str, Any]], claimed: set[str]) -> list[dict[str, Any]]:
    """Payments naming this supplier, in the window, for a little more than
    the receipt: a tip or service added after the bill was printed."""
    near = []
    for t in open_tx:
        if t["url"] in claimed or not _named(t, rec.token):
            continue
        paid = _pence(-float(t["amount"]))
        if rec.pence < paid <= rec.pence * (1 + NEAR_MISS_SHARE) + 50 \
                and -DAYS_BEFORE <= _days(t, rec.when) <= DAYS_AFTER:
            near.append({**t, "difference": (paid - rec.pence) / 100})
    return sorted(near, key=lambda t: t["difference"])
