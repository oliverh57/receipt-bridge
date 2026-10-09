"""What the Match screen says about a receipt (PLAN.md §11).

One pure function, `review()`, turns a receipt, its match and a few
settings into: the group it belongs in (Needs you / Waiting for bank /
Ready / Filed), the reason in a few words, the check chips, the payment
chips, and the "Will file as" summary. The UI shows these as they are;
the wording lives here so it stays short and consistent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .filer import EXEMPT, MANUAL, OUT_OF_SCOPE, vat_parts
from .matcher import DAYS_AFTER, DAYS_BEFORE, Match

CURRENCY_WORDS = {"EUR": "euros", "USD": "dollars", "AUD": "dollars", "CAD": "dollars",
                  "NZD": "dollars", "JPY": "yen", "DKK": "kroner", "SEK": "kronor",
                  "NOK": "kroner", "CHF": "francs", "ARS": "pesos", "MXN": "pesos"}

# Photo flags (app/photo_inbox.py) → a few words for the list.
FLAG_REASONS = (
    ("same purchase", "Possible duplicate"),
    ("Several totals", "Split bill?"),
    ("Total couldn't be read", "No total"),
    ("Total not confirmed", "Check the total"),
    ("Which currency", "Which currency?"),
    ("No date found", "No date"),
    ("No date on the receipt", "No date"),
    ("after the photo", "Check the date"),
    ("photographed", "Check the date"),
    ("swapped", "Check the date"),
    ("future", "Check the date"),
    ("18 months", "Over 18 months old"),
    ("no VAT number", "VAT, no VAT number"),
    ("VAT not found", "Check the VAT"),
    ("duplicate or reprint", "Reprint"),
    ("personally?", "Who paid?"),
    ("Supplier name", "Check supplier"),
)


# Problems with the file itself (Files), as opposed to finding its payment
# (Statement) or claiming it (Expenses).
FILE_ISSUES = ("Not a receipt?", "No total", "Possible duplicate", "Split bill?", "Which currency?",
               "No date", "Check the date", "Over 18 months old", "Who paid?", "Reprint",
               "Check the total", "Check supplier", "Check the VAT")


@dataclass
class Context:
    vat_registered: bool
    vat_treatment: str
    category_name: str
    waiting_days: int
    today: date


def _money(value: float | Decimal | None, currency: str | None = "GBP") -> str:
    if value is None:
        return "?"
    symbol = {"GBP": "£", "EUR": "€", "USD": "$", "JPY": "¥"}.get(currency or "", "")
    amount = f"{Decimal(str(value)):,.2f}"
    return f"{symbol}{amount}" if symbol else f"{amount} {currency or '?'}"


def _days_label(days: int | None) -> str:
    if days is None:
        return ""
    if days == 0:
        return "Same day"
    return f"{days:+d} day" + ("" if abs(days) == 1 else "s")


def _flags(row: Any) -> list[str]:
    try:
        return json.loads(row["extra_json"] or "{}").get("flags", []) if row["extra_json"] else []
    except ValueError:
        return []


def _extra(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
    except ValueError:
        return {}


def _explanation(t: dict[str, Any]) -> dict[str, Any] | None:
    """FreeAgent's one explanation of a payment, as cached, or None."""
    if not (t.get("explanation_url") and not t.get("unexplained_amount")):
        return None
    try:
        return json.loads(t.get("explanation_json") or "{}")
    except ValueError:
        return {}


def is_guess(t: dict[str, Any]) -> bool:
    """FreeAgent guessed this payment's explanation and it waits for approval:
    filing replaces it with an approved one (service._replace_guess)."""
    return (_explanation(t) or {}).get("marked_for_review") is True


def explained_for_good(t: dict[str, Any]) -> bool:
    """Explained in FreeAgent and not a guess: filing only attaches the receipt."""
    return _explanation(t) is not None and not is_guess(t)


def is_undated(row: Any) -> bool:
    """The date stands in from the photo: the receipt didn't show one."""
    return any(("No date on the receipt" in f or "No date found" in f) for f in _flags(row))


def review(row: Any, match: Match | None, ctx: Context, *, overdue: bool) -> dict[str, Any]:
    flags = _flags(row)
    extra = _extra(row)
    personal = row["paid_by"] == "personal"
    currency = row["currency"]
    total = row["total"]

    # ---- why it needs you (first that applies) ----
    reasons: list[str] = []
    if extra.get("not_a_receipt"):
        reasons.append("Not a receipt?")
    if match and match.status == "expense_but_found":
        reasons.append("Paid from the business?")
    if match and match.status == "choose":
        reasons.append("Which day?")         # no date, several identical payments
    if total is None:
        reasons.append("No total")
    if match and match.status == "waiting" and (match.options or overdue):
        reasons.append(f"No {_money(total, currency)} payment")
    if personal and currency and currency != "GBP":
        reasons.append(f"Expense in {CURRENCY_WORDS.get(currency, currency)}")
    for needle, words in FLAG_REASONS:
        if any(needle in f for f in flags) and words not in reasons:
            reasons.append(words)
    if not row["category"] and not ctx.category_name and not (
            match and match.transaction and match.transaction.get("explanation_url")
            and not match.transaction.get("unexplained_amount")):
        reasons.append("Choose a category")

    waiting = bool(match and match.status == "waiting" and not match.options and not overdue)
    if row["status"] == "filed":
        group = "filed"
    elif reasons:
        group = "needs"
    elif waiting:
        group = "waiting"
    else:
        group = "ready"

    # ---- check chips (§5.3) ----
    checks = []
    if total is not None:
        trusted = row["source"] != "photo" or (row["total_status"] or "").startswith("confirmed")
        checks.append({"name": "Total" if trusted else "Check total", "ok": trusted})
    if row["vat"]:
        checks.append({"name": "VAT", "ok": True})
        checks.append({"name": "VAT number" if row["vat_number"] else "No VAT number",
                       "ok": bool(row["vat_number"])})
    elif row["vat_number"]:
        checks.append({"name": "VAT number", "ok": True})
    undated = is_undated(row)
    checks.append({"name": "No date" if undated or not row["purchased_on"] else "Date",
                   "ok": not undated and bool(row["purchased_on"])})
    if match and match.status == "waiting" and (match.options or overdue):
        checks.append({"name": "No exact payment", "ok": False})
    if personal and currency and currency != "GBP":
        checks.append({"name": "Not GBP", "ok": False})

    # ---- the payment ----
    payment = None
    if match and match.transaction is not None:
        t = match.transaction
        chips = []
        if match.exact:
            chips.append("Amount")
        else:
            more = -float(t["amount"]) - float(total or 0)
            chips.append(f"{'+' if more > 0 else '−'}{_money(abs(more))}")
        if match.alias_hit:
            chips.append("Name")
        if match.days is not None:
            far = not -DAYS_BEFORE <= match.days <= DAYS_AFTER
            # within the days a card takes to settle: just "Date"; further
            # apart (a payment you chose) says by how much
            chips.append(_days_label(match.days) if far else "Date")
        payment = {"description": (t.get("description") or "").split("//")[0], "date": t["dated_on"],
                   "amount": float(t["amount"]), "chips": chips, "pinned": match.pinned,
                   # further apart than a card usually takes to settle (you chose it)
                   "far": match.days is not None and not -DAYS_BEFORE <= match.days <= DAYS_AFTER,
                   "explained": explained_for_good(t), "guess": is_guess(t),
                   # you chose this one, but this other payment is the exact amount
                   "better": None if match.better is None else {
                       "url": match.better["url"], "date": match.better["dated_on"],
                       "amount": float(match.better["amount"]),
                       "description": (match.better.get("description") or "").split("//")[0]}}
    options = []
    if match and match.options:
        for t in match.options:
            options.append({"url": t["url"], "date": t["dated_on"], "amount": float(t["amount"]),
                            "description": (t.get("description") or "").split("//")[0],
                            "difference": t.get("difference")})

    # ---- will file as ----
    if payment and payment["explained"]:
        kind = "Attach to FreeAgent's explanation"
    elif payment and payment["guess"]:
        kind = "Bank explanation, in place of FreeAgent's guess"
    elif personal:
        kind = "Expense claim"
    else:
        kind = "Bank explanation"
    vat_text = _vat_text(row, ctx, match)
    attachment = {"photo": "Photo", "supplier": "Supplier PDF"}.get(
        "photo" if row["source"] == "photo" else ("supplier" if row["pdf_source"] not in ("rendered_email", None) else ""),
        "Email PDF")

    # ---- where it is in Files: check / link / waiting / expense (ready to claim) ----
    exact_named = bool(match and match.transaction is not None and match.exact and match.alias_hit)
    issues = [r for r in reasons if r in FILE_ISSUES
              # a bank payment of exactly this amount, naming the supplier, settles these
              and not (exact_named and r in ("Check the total", "Check supplier"))]
    if row["status"] == "filed":
        stage = "filed"
    elif issues:                       # expenses too: everything is checked in Files
        stage = "check"
    elif personal:
        stage = "expense"
    elif match and (match.transaction is not None or match.options):
        stage = "link"
    else:
        stage = "waiting"

    receipt_day = None
    if row["purchased_on"]:
        try:
            receipt_day = date.fromisoformat(row["purchased_on"][:10])
        except ValueError:
            receipt_day = None
    return {
        "group": group,
        "reason": reasons[0] if reasons else ("Waiting for bank" if waiting else ""),
        "reasons": reasons,
        "stage": stage,
        "issues": issues,
        "checks": checks,
        "payment": payment,
        "options": options,
        # its exact payment already has a receipt in FreeAgent: probably this one, filed before
        "in_freeagent": None if not match or match.in_freeagent is None else {
            "date": match.in_freeagent["dated_on"], "amount": float(match.in_freeagent["amount"]),
            "description": (match.in_freeagent.get("description") or "").split("//")[0]},
        "waiting_until": (receipt_day + timedelta(days=ctx.waiting_days)).isoformat()
        if waiting and receipt_day else None,
        "will_file": {"type": kind, "category": ctx.category_name or None, "vat": vat_text,
                      "attachment": attachment},
        "ai_guess": {"supplier": extra.get("supplier_source") in ("model", "first line")},
    }


def _extra_choice(row: Any) -> str:
    try:
        return json.loads(row["extra_json"] or "{}").get("vat_choice") or "auto"
    except (TypeError, ValueError):
        return "auto"


def _vat_text(row: Any, ctx: Context, match: Match | None) -> str:
    if not ctx.vat_registered:
        return "Not VAT registered"
    if ctx.vat_treatment == "reverse_charge":
        return "Reverse charge"
    if row["currency"] and row["currency"] != "GBP":
        return "£0 (foreign VAT)"
    if row["total"] is None:
        return "?"
    parts, problem = vat_parts(row, Decimal(str(row["total"])), True)
    if problem:
        return "Check the VAT"
    texts = []
    for gross, rate in parts:
        if rate in (EXEMPT, OUT_OF_SCOPE):
            texts.append("Exempt" if rate == EXEMPT else "Out of scope")
            continue
        if rate and rate.startswith(MANUAL):
            texts.append(f"£{rate[len(MANUAL):]} (amount)")      # FreeAgent's "Amount…"
            continue
        r = Decimal(rate or "0")
        if r == 0:
            none_shown = len(parts) == 1 and (_extra_choice(row) == "auto")
            texts.append("£0 (none shown)" if none_shown else "£0 (0%)")
        else:
            texts.append(f"£{(gross * r / (100 + r)).quantize(Decimal('0.01'))} ({int(r)}%)")
    text = " + ".join(texts)
    if match and match.transaction is not None and not match.exact:
        tip = -float(match.transaction["amount"]) - float(row["total"])
        if tip > 0:                     # a smaller payment isn't filed at all
            text += f" + {_money(tip)} tip (0%)"
    return text
