"""Build a supplier rule from one example email, so nobody writes YAML.

The user picks a receipt from their inbox. This module proposes everything a
rule needs — who it's from, which number is the total, which is the
reference, whether there's a PDF — and the user just confirms.

The interesting part is turning a *value* into a *pattern*. Matching the
literal "£12.34" would only ever find that one receipt. Instead each pattern
is anchored on the label printed beside the value ("Total:", "Order #") and
captures whatever number follows it, so the rule keeps working when next
month's amount differs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import yaml

from .email_message import Email
from .watchers import Watcher, build_filename

# A money amount, with its currency either side: "£12.34", "12.34 GBP",
# "GBP 12.34" (QuickBooks), "€9".
MONEY = re.compile(
    r"(?P<sym>[£$€])\s?(?P<a>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?!\d)"
    r"|(?P<b>\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\s?(?P<code>GBP|EUR|USD)\b"
    r"|\b(?P<pre>GBP|EUR|USD)\s?(?P<c>\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})(?!\d)"
)
# A figure with no currency at all — "TOTAL 90.00" in an invoice's table.
# Only worth offering when a total word or VAT labels it; otherwise every
# rate and quantity on the page would be a candidate. Dates like 01.10.2026 are not.
BARE = re.compile(r"(?<![\d.,£$€])(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})(?!\.?\d|%)")
SYMBOL_CURRENCY = {"£": "GBP", "€": "EUR", "$": "USD"}
NUMBER = r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)"

# Words that mark the figure that matters. Order is preference.
TOTAL_WORDS = ("total", "amount paid", "amount charged", "grand total", "paid", "charged", "amount",
               "balance due")
# Labels for a part of the total rather than the total itself: "SUBTOTAL",
# "VAT TOTAL", "Net amount", "Total excl. VAT". "Total incl. VAT" is the total.
PART_OF_TOTAL = re.compile(r"sub[\s-]?total|\b(?:vat|tax|net|ex|excl|excluding)\b", re.I)
# A VAT figure: "VAT", "VAT total", "Total VAT: £15.00". Not "Total incl.
# VAT", "Total excl. VAT" or "VAT number"; and not US sales tax, never VAT.
VAT_LABEL = re.compile(r"\bvat\b", re.I)
NOT_VAT_LABEL = re.compile(r"\b(?:inc|ex|net|no\b|num|reg)", re.I)

# Labelled identifiers: "Order #123-456", "Invoice number: INV-0042".
REFERENCE = re.compile(
    r"(?P<label>(?:order|invoice|receipt|transaction|booking|payment|reference|ref|confirmation)"
    r"(?:\s+(?:id|no\.?|number|ref(?:erence)?))?)\s*[:#]?\s*#?\s*"
    r"(?P<value>(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{4,})",
    re.IGNORECASE,
)

# Senders that tell you nothing about who the supplier is.
GENERIC_SENDER_WORDS = {"noreply", "no-reply", "donotreply", "receipts", "receipt", "billing",
                        "orders", "order", "info", "hello", "support", "team", "mail", "notifications"}


@dataclass
class Candidate:
    value: str
    label: str
    pattern: str
    context: str
    currency: str = ""
    score: int = 0
    source: str = "text"  # or "attachment_text" when found in an attached PDF
    currency_shown: bool = True  # False: a bare figure, in the document's currency


def _vat_label(label: str) -> bool:
    return bool(VAT_LABEL.search(label)) and not NOT_VAT_LABEL.search(label)


@dataclass
class Analysis:
    name: str
    sender: str
    domain: str
    subject: str
    subject_hint: str
    amounts: list[Candidate] = field(default_factory=list)
    vat_amounts: list[Candidate] = field(default_factory=list)
    references: list[Candidate] = field(default_factory=list)
    has_pdf: bool = False
    pdf_name: str = ""


def analyse(message: Email, sibling_subjects: list[str] | tuple[str, ...] = ()) -> Analysis:
    display, address = parseaddr(message.sender)
    domain = address.split("@")[-1].lower() if "@" in address else ""
    pdfs = message.pdf_attachments()
    sources = [("text", message.text)]
    if pdfs:
        sources.append(("attachment_text", message.attachment_text))
    found = [c for src, t in sources for c in _amounts(t, src)]
    return Analysis(
        name=_supplier_name(display, domain),
        sender=address,
        domain=_registrable(domain),
        subject=message.subject,
        subject_hint=_subject_hint(message.subject, sibling_subjects),
        amounts=_ranked(found),
        # The biggest VAT figure first: a "VAT total" over its per-rate lines.
        vat_amounts=sorted(_dedupe(c for c in found if _vat_label(c.label)),
                           key=lambda c: -float(c.value.replace(",", "")))[:4],
        references=_dedupe(c for src, t in sources for c in _references(t, src))[:6],
        has_pdf=bool(pdfs),
        pdf_name=pdfs[0]["filename"] if pdfs else "",
    )


def _registrable(domain: str) -> str:
    """mail.uber.com -> uber.com; email.amazon.co.uk -> amazon.co.uk."""
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "gov", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


_LEGAL_SUFFIX = re.compile(
    r"[,\s]+(PBC|Inc\.?|Ltd\.?|Limited|LLC|L\.L\.C\.|GmbH|plc|PLC|S\.?A\.?|B\.?V\.?|AG|Co\.?)$"
)


def _supplier_name(display: str, domain: str) -> str:
    name = _supplier_name_raw(display, domain)
    return _LEGAL_SUFFIX.sub("", name).strip(" ,") or name


def _supplier_name_raw(display: str, domain: str) -> str:
    words = [w for w in re.split(r"[\s|·\-–—]+", display) if w]
    useful = [w for w in words if w.lower().strip(".,") not in GENERIC_SENDER_WORDS]
    if useful:
        return " ".join(useful[:3])
    root = _registrable(domain).split(".")[0] if domain else "Supplier"
    return root.capitalize()


_DATE_WORDS = re.compile(
    r"\b(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|"
    r"oct(ober)?|nov(ember)?|dec(ember)?|mon(day)?|tue(sday)?|wed(nesday)?|thu(rsday)?|"
    r"fri(day)?|sat(urday)?|sun(day)?)\b",
    re.IGNORECASE,
)


def _clean_subject(subject: str) -> str:
    """The parts of a subject that can't be the same next time are removed:
    quoted product names, everything from the first number on, and dates.
    """
    text = re.sub(r"[‘“\"][^’”\"]*[’”\"]?", " ", subject)
    first_digit = re.search(r"\d", text)
    if first_digit:
        text = text[: first_digit.start()]
    text = _DATE_WORDS.sub(" ", text)
    return " ".join(text.split())


def _subject_hint(subject: str, others: list[str] | tuple[str, ...] = ()) -> str:
    """The fixed part of a supplier's subject lines.

    Guessing from one subject proposed the variable part as often as not —
    the product name of an Amazon order, the month of a FreeAgent invoice —
    and the rule then matched nothing next time. So when the sender's other
    recent subjects are available, keep only the words they share with this
    one: "Ordered", "Your booking confirmation for".
    """
    mine = _clean_subject(subject).split()
    if not mine:
        return ""
    # Compare only with subjects of the same kind — sharing the first three
    # words. Matching on the first word alone let "FreeAgent - New login…"
    # shrink FreeAgent's hint to just "FreeAgent", which would then catch
    # security emails too.
    kind = [w.lower() for w in mine[:3]]
    siblings = [_clean_subject(o).split() for o in others if o and o != subject]
    siblings = [w for w in siblings if [x.lower() for x in w[: len(kind)]] == kind]

    prefix = mine
    for words in siblings:
        n = 0
        while n < min(len(prefix), len(words)) and prefix[n].lower() == words[n].lower():
            n += 1
        prefix = prefix[:n]
    hint = " ".join(prefix).strip(" -:–—|·,#№(")
    return hint[:60] if len(hint) >= 4 else ""


def _label_before(line: str, start: int) -> str:
    """The words immediately before a value on its line: "Total amount:"."""
    before = line[:start]
    match = re.search(r"([A-Za-z][A-Za-z &'/()-]{1,40}?)[\s:#.]*$", before)
    if not match:
        return ""
    words = match.group(1).strip().split()
    return " ".join(words[-3:])


def _ranked(candidates) -> list[Candidate]:
    merged = _dedupe(candidates)
    # Labelled totals first, then larger amounts — a receipt's total is
    # usually its biggest figure.
    merged.sort(key=lambda c: (-c.score, -float(c.value.replace(",", ""))))
    return merged[:8]


def _dedupe(candidates) -> list[Candidate]:
    seen: dict[tuple[str, str], Candidate] = {}
    for c in candidates:
        seen.setdefault((c.label.lower(), c.value), c)
    return list(seen.values())


def _label_line(line: str) -> str:
    """A line that is nothing but a label: "Total amount:", "Payment ID"."""
    stripped = line.strip().rstrip(":#").strip()
    if not stripped or any(ch.isdigit() for ch in stripped):
        return ""
    words = stripped.split()
    return stripped if len(words) <= 4 and len(stripped) <= 40 else ""


def _label_pattern(label: str, starts_line: bool) -> str:
    """The label as a regex that can't match inside a longer one.

    Patterns run case-insensitively against the whole text and take the first
    hit, so a bare "Total" would find "Subtotal £75.00" or "VAT Total £15.00"
    before the real total. A label that begins its line is pinned there.
    """
    if starts_line:
        return r"(?<![^\n])[ \t]*" + re.escape(label)
    return (r"\b" if label[:1].isalnum() else "") + re.escape(label)


def _total_score(label: str) -> int:
    """How surely a label marks the total: 0 for none of the total words, and
    low for a part of the total, so a subtotal is offered after the total."""
    lowered = label.lower()
    score = next((100 - i * 5 for i, word in enumerate(TOTAL_WORDS) if word in lowered), 0)
    if score and PART_OF_TOTAL.search(lowered) and not re.search(r"\binc", lowered):
        return 10
    return score


def _amounts(text: str, source: str = "text") -> list[Candidate]:
    found: dict[tuple[str, str], Candidate] = {}
    lines = [l for l in text.splitlines() if l.strip()]
    tagged = [SYMBOL_CURRENCY.get(m.group("sym") or "") or m.group("code") or m.group("pre")
              for m in MONEY.finditer(text)]
    # An unmarked figure is in whatever currency the rest of the document uses.
    document_currency = max(set(tagged), key=tagged.count) if tagged else ""
    for i, line in enumerate(lines):
        matches = list(MONEY.finditer(line))
        taken = [m.span() for m in matches]
        bare = [m for m in BARE.finditer(line)
                if not any(s <= m.start() < e for s, e in taken)]
        for m in matches + bare:
            label = _label_before(line, m.start())
            starts_line = bool(label) and re.sub(r"[\s:#.]*$", "", line[: m.start()]).strip() == label
            if not label and i and not line[: m.start()].strip():
                # Patterns join label and value with \s*, which spans the
                # line break, so the rule still works on the flattened table.
                label = _label_line(lines[i - 1])
                starts_line = bool(label)
            score = _total_score(label)
            if m.re is BARE:
                if not score and not _vat_label(label):
                    continue
                raw, currency, money = m.group(1), document_currency, NUMBER + r"(?!\.?\d|%)"
            elif m.group("sym"):
                raw, currency = m.group("a"), SYMBOL_CURRENCY[m.group("sym")]
                money = re.escape(m.group("sym")) + r"\s*" + NUMBER
            elif m.group("pre"):
                raw, currency = m.group("c"), m.group("pre")
                money = re.escape(m.group("pre")) + r"\s*" + NUMBER
            else:
                raw, currency = m.group("b"), m.group("code")
                money = NUMBER + r"\s*" + re.escape(m.group("code"))
            pattern = (_label_pattern(label, starts_line) + r"[\s:]*" + money) if label else money
            key = (label.lower(), raw)
            if key not in found:
                found[key] = Candidate(raw, label, pattern, line.strip()[:120], currency, score, source,
                                       currency_shown=m.re is not BARE)
    return list(found.values())


LABEL_ONLY = re.compile(
    r"^(?P<label>(?:order|invoice|receipt|transaction|booking|payment|reference|ref|confirmation)"
    r"(?:\s+(?:id|no\.?|number|ref(?:erence)?))?)\s*[:#]?$",
    re.IGNORECASE,
)
ID_VALUE = re.compile(r"^#?\s*((?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{4,})$", re.IGNORECASE)


def _references(text: str, source: str = "text") -> list[Candidate]:
    found: dict[str, Candidate] = {}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # Label alone on a line, identifier on the next.
    for label_line, value_line in zip(lines, lines[1:]):
        label_match, value_match = LABEL_ONLY.match(label_line), ID_VALUE.match(value_line)
        if label_match and value_match and value_match.group(1) not in found:
            label = label_match.group("label")
            pattern = re.escape(label) + r"\s*[:#]?\s*#?\s*([A-Z0-9][A-Z0-9-]{4,})"
            found[value_match.group(1)] = Candidate(
                value_match.group(1), label, pattern, f"{label_line} {value_line}"[:120], source=source
            )
    for line in lines:
        for m in REFERENCE.finditer(line):
            value, label = m.group("value"), m.group("label").strip()
            if value in found:
                continue
            pattern = re.escape(label) + r"\s*[:#]?\s*#?\s*([A-Z0-9][A-Z0-9-]{4,})"
            found[value] = Candidate(value, label, pattern, line.strip()[:120], source=source)
    return list(found.values())


# ---- turning choices into a rule ------------------------------------------


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "supplier"


def build_spec(choices: dict[str, Any]) -> dict[str, Any]:
    """A watcher definition from what the user confirmed."""
    name = (choices.get("name") or "").strip() or "Supplier"
    domain = (choices.get("domain") or "").strip().lower()
    subject = (choices.get("subject") or "").strip()
    amount = choices.get("amount_pattern") or ""
    currency = choices.get("currency") or "GBP"
    reference = choices.get("reference_pattern") or ""
    vat = choices.get("vat_pattern") or ""
    paid_with = (choices.get("paid_with") or "business").strip()

    if not domain:
        raise ValueError("Choose who the emails come from")
    if not amount:
        raise ValueError("Choose which amount is the total")
    if paid_with not in ("business", "personal") and not paid_with.startswith("https://"):
        raise ValueError("Paid with is a bank account, business or personal")

    from .supplier_editor import build_query

    query = build_query(domain, subject, choices.get("mentions") or "")
    match: dict[str, Any] = {"from_contains": domain}
    if subject:
        match["subject_contains"] = subject
    # For senders shared by many merchants — payment processors like ecommpay
    # or Stripe — the merchant's name is only in the body.
    mentions = (choices.get("mentions") or "").strip()
    if mentions:
        match["body_contains"] = mentions

    fields: dict[str, Any] = {
        "total": {
            "type": "money", "required": True, "patterns": [amount],
            "source": choices.get("amount_source") or "text",
        },
        "currency": {"value": currency},
        "purchased_on": {"type": "date", "default": "email_date", "patterns": []},
        "description": {"source": "subject", "patterns": [r"(.+)"]},
    }
    if vat:
        # Required: a month whose VAT can't be found is held for you to
        # check, rather than filed at 0% and the reclaim quietly lost.
        fields["vat"] = {
            "type": "money", "required": True, "patterns": [vat],
            "source": choices.get("vat_source") or "text",
        }
    if reference:
        fields = {
            "reference": {
                "required": True, "patterns": [reference],
                "source": choices.get("reference_source") or "text",
            },
            **fields,
        }

    pdf: list[Any] = [{"step": "attachment"}, "render_email"] if choices.get("use_attachment") else ["render_email"]
    filename = "{purchased_on} {vendor} {currency}{total}" + (" {reference}" if reference else "") + ".pdf"

    spec: dict[str, Any] = {
        "id": slug(name),
        "name": name,
        "vendor": name,
        "enabled": True,
        "created_by": "app",
        "gmail_query": query,
        "lookback_days": 365,
        "match": match,
        "fields": fields,
        "pdf": pdf,
        "filename": filename,
    }
    if paid_with != "business":   # the default, as the supplier editor writes it
        spec["paid_with"] = paid_with
    return spec


def preview(spec: dict[str, Any], message: Email) -> dict[str, Any]:
    """What the rule pulls from the example email, and the file it would make."""
    watcher = Watcher.from_dict(spec)
    if not watcher.matches(message):
        return {"ok": False, "problem": "The rule doesn't match the example email. Check the sender and subject."}
    try:
        values = watcher.extract(message)
    except Exception as exc:
        return {"ok": False, "problem": str(exc)}
    total, vat = values.get("total"), values.get("vat")
    vat_rate = ""
    if vat is not None and total:
        # What filing will do with it: a VAT figure that isn't 20% or 5% of
        # the total can't be filed, so say so now rather than every month.
        from decimal import Decimal

        from .filer import vat_rate as filing_rate

        rate, problem = filing_rate({"vat": vat, "extra_json": "{}"}, Decimal(str(total)), True)
        if problem:
            mark = "£" if values.get("currency") in (None, "GBP") else f"{values['currency']} "
            return {"ok": False, "problem": f"{mark}{vat:.2f} isn't VAT at 20% or 5% of {mark}{total:.2f}. "
                                            "Choose the VAT figure, or No VAT."}
        vat_rate = f"{float(rate):g}%"
    from .receipt_text import find_vat_number

    return {
        "ok": True,
        "total": total,
        "currency": values.get("currency"),
        "reference": values.get("reference") or "",
        "date": values.get("purchased_on"),
        "vat": vat,
        "vat_rate": vat_rate,
        "vat_number": find_vat_number(f"{message.text}\n{message.attachment_text}",
                                      values.get("currency")) or "",
        "filename": build_filename(watcher.filename, values),
    }


HEADER = """# Created in Receipt Bridge from an example email.
# Edit freely — or remove it in Settings. Field reference: watchers/README.md
"""


def save(spec: dict[str, Any], watchers_dir: Path) -> Path:
    """Write the rule, never overwriting one that already exists."""
    watchers_dir.mkdir(parents=True, exist_ok=True)
    base = spec["id"]
    path = watchers_dir / f"{base}.yaml"
    n = 2
    while path.exists():
        spec["id"] = f"{base}-{n}"
        path = watchers_dir / f"{spec['id']}.yaml"
        n += 1
    Watcher.from_dict(spec)  # refuse to write anything the loader would reject
    body = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(HEADER + body, encoding="utf-8")
    return path
