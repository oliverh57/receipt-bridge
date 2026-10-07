"""File a receipt into FreeAgent, safely (PLAN.md §7.3, §9).

  bank payment  → a bank transaction explanation on the matched
                  transaction (one per VAT rate on a mixed-rate receipt),
                  then the receipt attached to the first
  paid personally → an out-of-pocket expense with the receipt attached

Safety, in order:

* **Dry run** (the default): the exact request is built and stored for you
  to read; nothing is sent.
* **A plan is checked before anything is sent**: total, date, category,
  the transaction's amount, VAT, an attachable file. Any problem stops it.
* **Pre-flight**: the transaction is read back from FreeAgent just before
  writing and must still be wholly unexplained.
* **Intent first**: "filing" is recorded before the write, and every
  explanation carries `RB-<receipt id>` as its receipt reference, so an
  interrupted filing is matched up by reading FreeAgent, never re-posted
  blindly.
* **Undo**: only records whose URLs this app stored are ever deleted.

VAT is only ever what was printed. FreeAgent won't take a VAT amount, only
a rate, from which it works the amount out (confirmed in the sandbox), so a
receipt with two rates is split: each printed VAT line becomes its own
explanation at its rate, and whatever is left of the total goes at 0%. If
FreeAgent's own sum for a part wouldn't equal the printed VAT to the
penny, nothing is filed.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .db import FILED, PENDING, Database
from .freeagent import FreeAgent, FreeAgentError

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
CONTENT_TYPES = {".pdf": "application/x-pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png", ".gif": "image/gif"}
UK_RATES = (Decimal(20), Decimal(5))
PENNY = Decimal("0.01")


class FilingError(RuntimeError):
    pass


@dataclass
class Plan:
    kind: str                                   # "explanation", "expense" or "attach"
    body: dict[str, Any]                        # the first (often only) part
    attachment: dict[str, Any] | None           # file_name, content_type, description, path
    transaction_url: str | None = None
    problems: list[str] = field(default_factory=list)
    parts: list[dict[str, Any]] = field(default_factory=list)   # every explanation to create

    def preview(self) -> dict[str, Any]:
        """What would be sent, without the file's bytes."""
        return {"kind": self.kind, "body": self.body, "parts": self.parts or [self.body],
                "attachment": None if not self.attachment else
                {k: v for k, v in self.attachment.items() if k != "path"},
                "transaction": self.transaction_url}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pennies(value: Decimal) -> Decimal:
    return value.quantize(PENNY, rounding=ROUND_HALF_UP)


REBILL_FIELDS = ("project", "rebill_type", "rebill_factor")


def reference(receipt_id: int) -> str:
    return f"RB-{receipt_id}"


def _extra(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["extra_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def _vat_of(gross: Decimal, rate: Decimal) -> Decimal:
    """FreeAgent's VAT on a VAT-inclusive gross at a rate."""
    return _pennies(gross * rate / (100 + rate))


# The VAT you chose on a receipt, as FreeAgent offers it. Auto: as printed.
AUTO, AMOUNT, EXEMPT, OUT_OF_SCOPE = "auto", "amount", "EXEMPT", "OUT_OF_SCOPE"
FIXED_RATES = ("20.0", "5.0", "0.0")
VAT_CHOICES = (AUTO, AMOUNT, *FIXED_RATES, EXEMPT, OUT_OF_SCOPE)
MANUAL = "manual:"          # a part's "rate" when the VAT is a typed amount


def tax_fields(rate: str | None) -> dict[str, Any]:
    """A part's rate as FreeAgent's fields: a rate, Exempt, Out of Scope, or
    a VAT amount typed in (FreeAgent's "Amount...")."""
    if rate is None:
        return {}
    if rate in (EXEMPT, OUT_OF_SCOPE):
        return {"sales_tax_status": rate}
    if rate.startswith(MANUAL):
        return {"sales_tax_status": "TAXABLE", "manual_sales_tax_amount": rate[len(MANUAL):]}
    return {"sales_tax_rate": rate}


def _vat_from_layout(extra: dict[str, Any], gross: Decimal) -> tuple[Decimal | None, list[list[Any]]]:
    """The VAT printed on a document, read from its stored text rows."""
    rows = [" ".join(p.get("t", "") for p in line.get("pieces", []))
            for line in extra.get("layout") or [] if isinstance(line, dict)]
    if not rows:
        return None, []
    from .receipt_text import find_vat_lines

    vat, lines = find_vat_lines([r for r in rows if r.strip()], "GBP", gross)
    return vat, [[None if r is None else str(r), str(v), None if g is None else str(g)] for r, v, g in lines]


def vat_parts(row: Any, gross: Decimal, vat_registered: bool
              ) -> tuple[list[tuple[Decimal, str | None]], str | None]:
    """([(gross for this part, sales_tax_rate to send)], problem).

    Not VAT registered → one part, no rate. Nothing printed, or "not a VAT
    receipt" → one part at 0%. One rate fits the printed VAT → one part. Two
    rates → a part per printed VAT line plus the remainder at 0%."""
    if not vat_registered:
        return [(gross, None)], None
    extra = _extra(row)
    choice = extra.get("vat_choice") or AUTO
    if choice in FIXED_RATES:
        return [(gross, choice)], None
    if choice in (EXEMPT, OUT_OF_SCOPE):
        return [(gross, choice)], None
    if choice == AMOUNT:
        typed = Decimal(str(extra.get("vat_amount") or 0))      # Auto keeps what was read
        if not typed:
            return [(gross, "0.0")], None
        for rate in UK_RATES:
            if abs(typed - gross * rate / (100 + rate)) <= Decimal("0.02"):
                return [(gross, f"{rate}.0")], None
        if typed > _vat_of(gross, Decimal(20)) + PENNY:
            return [], f"VAT £{typed} is more than 20% of £{gross}."
        return [(gross, f"{MANUAL}{_pennies(typed)}")], None
    if any("not a VAT receipt" in f for f in extra.get("flags", [])):
        return [(gross, "0.0")], None
    printed = row["vat"] if row["vat"] is not None else extra.get("vat")
    if printed is None and (row["currency"] if "currency" in row.keys() else "GBP") in (None, "GBP"):
        # Nothing read when it arrived (a supplier rule with no VAT field):
        # read it now from the document's text, kept for its highlights
        printed, lines = _vat_from_layout(extra, gross)
        if printed is not None:
            extra = {**extra, "vat_lines": lines}
    if not printed:
        return [(gross, "0.0")], None
    vat = Decimal(str(printed))
    for rate in UK_RATES:
        if abs(vat - gross * rate / (100 + rate)) <= Decimal("0.02"):
            return [(gross, f"{rate}.0")], None

    parts: list[tuple[Decimal, str | None]] = []
    for line in extra.get("vat_lines") or []:
        rate = Decimal(line[0]) if line[0] else None
        line_vat = Decimal(line[1])
        if rate is None or rate == 0:
            continue
        line_gross = Decimal(line[2]) if line[2] else _pennies(line_vat * (100 + rate) / rate)
        if _vat_of(line_gross, rate) != line_vat:
            return [], (f"VAT £{line_vat} at {rate}% doesn't come out to the penny on £{line_gross}. "
                        "File this one in FreeAgent by hand.")
        parts.append((line_gross, f"{int(rate)}.0" if rate == rate.to_integral() else str(rate)))
    if not parts:
        return [], (f"VAT £{vat} doesn't fit a single UK rate on £{gross}, and the receipt doesn't "
                    "show its VAT per rate. File this one in FreeAgent by hand.")
    remainder = gross - sum((g for g, _ in parts), Decimal(0))
    if remainder < 0:
        return [], "The VAT lines add up to more than the total. File this one by hand."
    if remainder > 0:
        parts.append((remainder, "0.0"))
    return parts, None


def vat_rate(row: Any, gross: Decimal, vat_registered: bool) -> tuple[str | None, str | None]:
    """The single rate, when there is one (a mixed-rate receipt has none)."""
    parts, problem = vat_parts(row, gross, vat_registered)
    if problem:
        return None, problem
    if len(parts) > 1:
        return None, "This receipt has two VAT rates."
    return parts[0][1], None


def _attachment(row: Any) -> tuple[dict[str, Any] | None, str | None]:
    path = Path(row["pdf_path"]) if row["pdf_path"] else None
    if path is None or not path.exists():
        return None, "No document to attach"
    content_type = CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None:
        return None, f"FreeAgent can't take {path.suffix} files"
    if path.stat().st_size > MAX_ATTACHMENT_BYTES:
        return None, "The document is over FreeAgent's 5 MB limit"
    name = row["filename"] or path.name
    if not name.lower().endswith(path.suffix.lower()):
        name += path.suffix
    return {"file_name": name, "content_type": content_type,
            "description": f"Receipt ({reference(row['id'])})", "path": str(path)}, None


REVERSE_CHARGE = "reverse_charge"


def plan_for(row: Any, *, transaction: dict[str, Any] | None, category_url: str | None,
             user_url: str | None, vat_registered: bool, vat_treatment: str = "printed",
             allow_difference: bool = False, rebill: dict[str, Any] | None = None) -> Plan:
    """`vat_treatment` is the supplier's setting: "printed" (the default: VAT
    exactly as printed) or "reverse_charge" (an overseas supplier of
    services; your accountant's call, PLAN.md §6.2). FreeAgent accepts
    reverse charge only at 0% and only with EC VAT reporting switched on in
    its company settings (checked in the sandbox).

    `allow_difference`: you chose a payment bigger than the receipt (a tip
    added after the bill was printed). The receipt files as printed and the
    difference as its own explanation at 0%.

    `rebill`: {project, type, factor} to re-bill a client's project: at
    cost, with a markup (factor in percent; FreeAgent takes a fraction), or
    at a set price (factor in pounds, so only for a receipt saved as one
    entry)."""
    problems: list[str] = []
    total = Decimal(str(row["total"])) if row["total"] is not None else None
    if total is None:
        problems.append("No total")
    if not row["purchased_on"]:
        problems.append("No date")
    if not category_url:
        problems.append("Choose a category")
    attachment, problem = _attachment(row)
    if problem:
        problems.append(problem)
    description = f"{row['vendor'] or 'Receipt'}: {row['description'] or ''}".strip(": ")[:200]

    reverse = vat_registered and vat_treatment == REVERSE_CHARGE

    if row["paid_by"] == "personal":
        # Expenses take one VAT rate each, so a mixed-rate receipt becomes one
        # expense per rate, like explanations. Foreign currency: no UK VAT.
        gbp = (row["currency"] or "GBP") == "GBP"
        if reverse:
            split, problem = [(total or Decimal(0), "0.0")], None
        elif gbp:
            split, problem = vat_parts(row, total or Decimal(0), vat_registered)
        else:
            split, problem = [(total or Decimal(0), None)], None
        if problem:
            problems.append(problem)
        if not user_url:
            problems.append("FreeAgent user not read yet: press Refresh in Settings")
        parts = []
        for n, (gross, rate) in enumerate(split or [(total or Decimal(0), None)], start=1):
            body: dict[str, Any] = {
                "user": user_url, "category": category_url, "dated_on": row["purchased_on"],
                "gross_value": f"{-gross:.2f}", "currency": row["currency"] or "GBP",
                "description": description if len(split) < 2 else f"{description} ({n} of {len(split)})",
                "receipt_reference": reference(row["id"]),
            }
            if gbp:
                body.update(tax_fields(rate))
            if reverse:
                body["ec_status"] = "Reverse Charge"
            parts.append(body)
        problems += _apply_rebill(parts, rebill)
        native = row["native_gross"] if "native_gross" in row.keys() else None
        if native and not gbp:
            if len(parts) == 1:
                # the £ actually charged (from the card statement), not FreeAgent's rate
                parts[0]["native_gross_value"] = f"{-Decimal(str(native)):.2f}"
            else:
                problems.append("£ charged can't be split across VAT rates")
        return Plan("expense", parts[0], attachment, None, problems, parts)

    if transaction is not None and transaction.get("explanation_url") and \
            not transaction.get("unexplained_amount"):
        # FreeAgent has already explained this payment (a bank rule, an
        # accepted guess) and it has no receipt: attach ours. Its category
        # and VAT stay as they are; re-billing, if you chose it, is added.
        problems = [p for p in problems if p != "Choose a category"]
        if total is not None and _pennies(Decimal(str(-transaction["amount"]))) != total:
            problems.append("The bank payment's amount doesn't equal the receipt total")
        if not attachment:
            problems.append("Nothing to attach")
        body: dict[str, Any] = {"explanation": transaction["explanation_url"]}
        update: dict[str, Any] = {}
        if category_url and transaction.get("explanation_category") and \
                category_url != transaction["explanation_category"]:
            update["category"] = category_url           # you chose another category
        if rebill and rebill.get("project"):
            problems += _apply_rebill([update], rebill)
        if update:
            body["update"] = update
        return Plan("attach", body, attachment, transaction["url"], problems, [body])

    difference = Decimal(0)
    if transaction is None:
        problems.append("No bank payment matched yet")
    elif total is not None and _pennies(Decimal(str(-transaction["amount"]))) != total:
        paid = _pennies(Decimal(str(-transaction["amount"])))
        if allow_difference and paid > total:
            difference = paid - total
        else:
            problems.append("The bank payment's amount doesn't equal the receipt total")
    split, problem = (([(total or Decimal(0), "0.0")], None) if reverse
                      else vat_parts(row, total or Decimal(0), vat_registered))
    if problem:
        problems.append(problem)
    split = list(split or [(total or Decimal(0), None)])
    if difference:
        split.append((difference, "0.0" if vat_registered else None))
    parts = []
    for n, (gross, rate) in enumerate(split, start=1):
        body = {
            "bank_transaction": transaction["url"] if transaction else None,
            "dated_on": transaction["dated_on"] if transaction else row["purchased_on"],
            "gross_value": f"{-gross:.2f}",
            "category": category_url,
            "description": description if len(split) < 2 else f"{description} ({n} of {len(split)})",
            "receipt_reference": reference(row["id"]),
        }
        if difference and n == len(split):
            body["description"] = f"{description}: difference from the receipt (tip or service)"[:200]
        body.update(tax_fields(rate))
        if reverse:
            body["ec_status"] = "Reverse Charge"
        parts.append(body)
    problems += _apply_rebill(parts, rebill)
    return Plan("explanation", parts[0], attachment, transaction["url"] if transaction else None,
                problems, parts)


def _encoded(attachment: dict[str, Any]) -> dict[str, Any]:
    data = base64.b64encode(Path(attachment["path"]).read_bytes()).decode("ascii")
    return {"data": data, "file_name": attachment["file_name"],
            "content_type": attachment["content_type"], "description": attachment["description"]}


def _record(db: Database, receipt_id: int, state: dict[str, Any], **changes: Any) -> None:
    db.update_receipt(receipt_id, {"freeagent_json": json.dumps({**state, "at": _now()}), **changes})


def _previous(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["freeagent_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def _ours_on(client: FreeAgent, transaction_url: str | None, ref: str) -> list[dict[str, Any]]:
    """Explanations already on the transaction carrying our RB- reference:
    what an interrupted filing managed to create."""
    if not transaction_url:
        return []
    transaction = client.get_url(transaction_url)["bank_transaction"]
    return [e for e in transaction.get("bank_transaction_explanations", []) or []
            if isinstance(e, dict) and e.get("receipt_reference") == ref]


def _our_expenses(client: FreeAgent, dated_on: str, ref: str) -> list[str]:
    """After an interruption: expenses on that date carrying our RB- reference."""
    expenses = client.get_all("expenses", "expenses", {"from_date": dated_on, "to_date": dated_on})
    return [e["url"] for e in expenses if e.get("receipt_reference") == ref]


def file_receipt(client: FreeAgent, db: Database, receipt_id: int, plan: Plan, *, dry_run: bool) -> str:
    """File one receipt; returns a line for the activity log."""
    row = db.get_receipt(receipt_id)
    if plan.problems:
        raise FilingError("; ".join(plan.problems))
    if dry_run:
        _record(db, receipt_id, {"state": "dry_run", **plan.preview()})
        return f"Dry run: {plan.kind} for {row['vendor']} prepared, nothing sent"

    client.writes_allowed = True
    try:
        if plan.kind == "expense":
            parts = plan.parts or [plan.body]
            state = _previous(row)
            urls: list[str] = list(state.get("urls") or [])
            if state.get("state") == "filing" and len(urls) < len(parts):
                found = _our_expenses(client, plan.body["dated_on"], reference(receipt_id))
                urls = urls + [u for u in found if u not in urls]
            for n, part in enumerate(parts[len(urls):], start=len(urls)):
                body = dict(part)
                if n == 0 and plan.attachment:          # the receipt goes on the first
                    body["attachment"] = _encoded(plan.attachment)
                _record(db, receipt_id, {"state": "filing", "kind": "expense", "urls": urls,
                                         "url": urls[0] if urls else None})
                urls.append(client.create_expense(body)["url"])
            _record(db, receipt_id, {"state": "filed", "kind": "expense", "url": urls[0], "urls": urls},
                    status=FILED, filed_at=_now())
            split = f", split across {len(urls)} VAT rates" if len(urls) > 1 else ""
            return f"Filed {row['vendor']} as an expense{split}"

        if plan.kind == "attach":
            explanation_url = plan.body["explanation"]
            transaction = client.get_url(plan.transaction_url)["bank_transaction"]
            current = next((e for e in transaction.get("bank_transaction_explanations") or []
                            if isinstance(e, dict) and e.get("url") == explanation_url), None)
            if current is None:
                raise FilingError("FreeAgent's explanation of that payment has changed; refresh and try again")
            if current.get("attachments") or current.get("attachment"):
                raise FilingError("That payment already has a receipt attached in FreeAgent")
            if current.get("is_locked"):
                raise FilingError("That explanation is locked in FreeAgent (a filed VAT return?)")
            update = plan.body.get("update")
            # what it had before, so Undo can put it back
            before = None
            if update:
                keys = set(update) | (set(REBILL_FIELDS) if "project" in update else set())
                before = {k: current.get(k) for k in sorted(keys)}
            _record(db, receipt_id, {"state": "filing", "kind": "attach", "url": explanation_url,
                                     "transaction": plan.transaction_url, "before": before})
            if update:
                client.update_explanation(explanation_url, update)
            attached = client.add_explanation_attachments(explanation_url, [_encoded(plan.attachment)])
            _record(db, receipt_id, {"state": "filed", "kind": "attach", "url": explanation_url,
                                     "transaction": plan.transaction_url, "before": before,
                                     "attachments": [a.get("url") for a in attached]},
                    status=FILED, filed_at=_now())
            changed = [w for w, on in (("its category", "category" in (update or {})),
                                       ("its project", "project" in (update or {}))) if on]
            rebilled = f" and changed {' and '.join(changed)}" if changed else ""
            return f"Attached {row['vendor']} to FreeAgent's existing explanation of the {transaction['dated_on']} payment{rebilled}"

        ref = reference(receipt_id)
        state = _previous(row)
        parts = plan.parts or [plan.body]
        urls: list[str] = list(state.get("urls") or ([state["url"]] if state.get("url") else []))
        if state.get("state") == "filing" and len(urls) < len(parts):
            # Interrupted: adopt whatever FreeAgent has with our reference.
            found = [e["url"] for e in _ours_on(client, plan.transaction_url, ref)]
            urls = urls + [u for u in found if u not in urls]
        if not urls and state.get("state") not in ("filing", "explained"):
            transaction = client.get_url(plan.transaction_url)["bank_transaction"]
            if Decimal(str(transaction.get("unexplained_amount", 0))) != Decimal(str(transaction["amount"])):
                raise FilingError("That payment is already (partly) explained in FreeAgent")
        for body in parts[len(urls):]:
            _record(db, receipt_id, {"state": "filing", "kind": "explanation", "urls": urls,
                                     "url": urls[0] if urls else None, "transaction": plan.transaction_url})
            urls.append(client.create_explanation(body)["url"])
        _record(db, receipt_id, {"state": "explained", "kind": "explanation", "url": urls[0],
                                 "urls": urls, "transaction": plan.transaction_url})
        attached = []
        if plan.attachment and not state.get("attachments"):
            attached = client.add_explanation_attachments(urls[0], [_encoded(plan.attachment)])
        _record(db, receipt_id, {"state": "filed", "kind": "explanation", "url": urls[0], "urls": urls,
                                 "transaction": plan.transaction_url,
                                 "attachments": [a.get("url") for a in attached]},
                status=FILED, filed_at=_now())
        split = f", split across {len(urls)} VAT rates" if len(urls) > 1 else ""
        return f"Filed {row['vendor']} against the {plan.body['dated_on']} payment{split}"
    finally:
        client.writes_allowed = False


def explain_without_receipt(client: FreeAgent, transaction_url: str, body: dict[str, Any]) -> str:
    """"No receipt needed" with a category: explain the whole payment in
    FreeAgent, nothing attached. Only a payment that is still wholly
    unexplained there. Returns the new explanation's URL."""
    transaction = client.get_url(transaction_url)["bank_transaction"]
    if Decimal(str(transaction.get("unexplained_amount", 0))) != Decimal(str(transaction["amount"])):
        raise FilingError("That payment is already (partly) explained in FreeAgent")
    client.writes_allowed = True
    try:
        return client.create_explanation(body)["url"]
    finally:
        client.writes_allowed = False


# What an explanation says, as FreeAgent takes it back on create: enough to
# put a removed one back as it was (except that it's no longer a guess).
RESTORE_FIELDS = ("bank_transaction", "dated_on", "gross_value", "category", "description",
                  "sales_tax_rate", "second_sales_tax_rate", "sales_tax_status", "ec_status",
                  "place_of_supply", "project", "rebill_type", "rebill_factor", "receipt_reference",
                  "transfer_bank_account", "paid_user", "paid_invoice", "paid_bill",
                  "foreign_currency_value", "stock_item", "stock_altering_quantity", "asset_life_years",
                  "disposed_asset", "property")
# Explanations that aren't spending: a receipt can't take their place.
NOT_SPENDING = ("transfer_bank_account", "paid_user", "paid_invoice", "paid_bill", "disposed_asset")


def removable_explanation(client: FreeAgent, transaction_url: str, explanation_url: str) -> dict[str, Any]:
    """FreeAgent's explanation of a payment, read live, if it's the payment's
    only one and may be removed. Raises FilingError saying why not."""
    transaction = client.get_url(transaction_url)["bank_transaction"]
    explanations = [e for e in transaction.get("bank_transaction_explanations") or [] if isinstance(e, dict)]
    if len(explanations) != 1 or explanations[0].get("url") != explanation_url:
        raise FilingError("FreeAgent's explanation of that payment has changed; refresh and try again")
    current = explanations[0]
    if current.get("is_locked"):
        raise FilingError("That explanation is locked in FreeAgent (a filed VAT return?)")
    if current.get("is_deletable") is False:
        raise FilingError("FreeAgent won't let that explanation be removed")
    if current.get("attachment") or current.get("attachments"):
        raise FilingError("That explanation has a receipt attached in FreeAgent; change it there")
    return current


def remove_explanation(client: FreeAgent, explanation: dict[str, Any]) -> None:
    """Delete an explanation checked by `removable_explanation`."""
    client.writes_allowed = True
    try:
        client.delete(explanation["url"])
    finally:
        client.writes_allowed = False


def restore_body(explanation: dict[str, Any]) -> dict[str, Any]:
    return {k: explanation[k] for k in RESTORE_FIELDS if explanation.get(k) not in (None, "")}


def create_explanation(client: FreeAgent, body: dict[str, Any]) -> str:
    client.writes_allowed = True
    try:
        return client.create_explanation(body)["url"]
    finally:
        client.writes_allowed = False


def update_existing_explanation(client: FreeAgent, transaction_url: str, explanation_url: str,
                                changes: dict[str, Any]) -> dict[str, Any]:
    """Change FreeAgent's own explanation of a payment (its category, VAT
    rate, re-billing). Returns what those fields were, for Undo."""
    transaction = client.get_url(transaction_url)["bank_transaction"]
    current = next((e for e in transaction.get("bank_transaction_explanations") or []
                    if isinstance(e, dict) and e.get("url") == explanation_url), None)
    if current is None:
        raise FilingError("FreeAgent's explanation of that payment has changed; refresh and try again")
    if current.get("is_locked"):
        raise FilingError("That explanation is locked in FreeAgent (a filed VAT return?)")
    before = {k: current.get(k) for k in changes}
    client.writes_allowed = True
    try:
        client.update_explanation(explanation_url, changes)
    finally:
        client.writes_allowed = False
    return before


def update_claim(client: FreeAgent, db: Database, receipt_id: int, plan: Plan, *, dry_run: bool) -> str:
    """Change an expense already claimed, in place: FreeAgent keeps the same
    entries, with their receipt attached. A different VAT split can't be
    changed in place, so that one means undoing the claim and filing again."""
    row = db.get_receipt(receipt_id)
    state = _previous(row)
    if state.get("state") != "filed" or state.get("kind") != "expense":
        raise FilingError("That receipt isn't claimed as an expense")
    if plan.kind != "expense":
        raise FilingError("It's no longer set as paid personally: undo the claim instead")
    if plan.problems:
        raise FilingError("; ".join(plan.problems))
    urls = state.get("urls") or [state["url"]]
    parts = plan.parts or [plan.body]
    if len(parts) != len(urls):
        raise FilingError("The VAT split has changed: undo the claim, then file it again")
    if dry_run:
        _record(db, receipt_id, {**state, "update": {"state": "dry_run", "parts": parts, "at": _now()}})
        return f"Dry run: the change to {row['vendor']}'s expense was prepared, nothing sent"
    client.writes_allowed = True
    try:
        for url, part in zip(urls, parts):
            client.update_expense(url, part)
    finally:
        client.writes_allowed = False
    _record(db, receipt_id, {**state, "update": {"state": "updated", "parts": parts, "at": _now()}})
    return f"Updated {row['vendor']}'s expense in FreeAgent"


def unfile(client: FreeAgent, db: Database, receipt_id: int) -> str:
    """Delete what this app created for the receipt, and put it back in To file."""
    row = db.get_receipt(receipt_id)
    state = _previous(row)
    if state.get("kind") == "attach":
        # We only added an attachment (and perhaps re-billing) to FreeAgent's
        # own explanation: remove those, and leave the rest alone.
        client.writes_allowed = True
        try:
            if state.get("attachments"):
                client.remove_explanation_attachments(state["url"], state["attachments"])
            if state.get("before"):
                client.update_explanation(state["url"], state["before"])
        finally:
            client.writes_allowed = False
        _record(db, receipt_id, {"state": "unfiled", "previous": state.get("attachments")},
                status=PENDING, filed_at=None)
        return f"Removed the receipt from {row['vendor']}'s explanation in FreeAgent"
    urls = state.get("urls") or ([state["url"]] if state.get("url") else [])
    if not urls:
        raise FilingError("Nothing this app created is recorded for that receipt")
    client.writes_allowed = True
    try:
        for url in urls:
            try:
                client.delete(url)
            except FreeAgentError as exc:
                if "(404)" not in str(exc):         # already gone in FreeAgent is fine
                    raise
    finally:
        client.writes_allowed = False
    _record(db, receipt_id, {"state": "unfiled", "previous": urls}, status=PENDING, filed_at=None)
    return f"Unfiled {row['vendor']}"


def _apply_rebill(parts: list[dict[str, Any]], rebill: dict[str, Any] | None) -> list[str]:
    """Link each entry to the project and add re-billing, unless it's "none"
    (linked, not re-billed). A set price can't be shared out between VAT
    rates, so it needs a receipt saved as one entry."""
    if not rebill or not rebill.get("project"):
        return []
    kind = rebill.get("type") or "cost"
    factor = rebill.get("factor")
    if kind in ("markup", "price") and not factor:
        return ["Re-bill: enter the " + ("markup %" if kind == "markup" else "price")]
    if kind == "price" and len(parts) > 1:
        return ["Re-bill at a set price works for a receipt saved as one entry: use at cost or a markup"]
    for body in parts:
        body["project"] = rebill["project"]
        if kind == "none":
            continue
        body["rebill_type"] = kind
        if kind == "markup":
            body["rebill_factor"] = format((Decimal(str(factor)) / 100).quantize(Decimal("0.0001")).normalize(), "f")
        elif kind == "price":
            body["rebill_factor"] = f"{Decimal(str(factor)):.2f}"
    return []
