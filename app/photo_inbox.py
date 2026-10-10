"""Bring receipt photos in from the iPhone (PLAN.md §4.2).

The "Receipt" Shortcut saves each photo into iCloud Drive:

    Receipt Inbox/Bank/      paid from the business account → bank explanation
    Receipt Inbox/Expense/   paid personally → out-of-pocket expense
    Receipt Inbox/           (loose) not asked; the question goes to review

iCloud is only a drop-off. Each file is copied into data/photos/ (the
app's archive of originals), the copy is checked byte for byte, then it's
read on this Mac, staged in To file, and removed from iCloud, so the free
5 GB is never at risk.

The copy shown and sent to FreeAgent is tidied when that's safe: cut to the
receipt, grey, contrast lifted (`tidy`). It's only used when reading it
again gives every value and amount the original gave; otherwise the plain
copy is used. The original is never changed. Anything that isn't a photo or PDF goes to
data/rejected/.

Nothing here guesses. What can't be read or doesn't add up becomes a flag
on the receipt for a person to settle in review.
"""

from __future__ import annotations

import hashlib
import re
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from .config import Config
from .db import FAILED, PENDING, Database
from .receipt_reader import PhotoReading, ReaderError, choose_copy, read_file, using_copy
from .receipt_text import CONFIRMED, UNCONFIRMED, ReceiptText, same_purchase

log = logging.getLogger(__name__)

PHOTO_TYPES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
READABLE = PHOTO_TYPES | {".pdf"}
PAID_BY = {"bank": "business", "expense": "personal"}
WATCHER_ID = "photo"
# A file still being written by iCloud changes; wait until it's been quiet.
SETTLE_SECONDS = 10
OLDEST_USEFUL = timedelta(days=548)        # ~18 months (PLAN.md §5.3)
# Any amount with pence ("3.40", "12,50"): every receipt has at least one.
_AMOUNT = re.compile(r"\d[.,]\d{2}(?!\d)")
# "Hackney Central to North Greenwich £2.30": a price in the supplier name
_POUNDS_IN_NAME = re.compile(r"£\s?(\d{1,5}(?:,\d{3})*\.\d{2})(?!\d)")
UNKNOWN_SUPPLIER = "Unknown supplier"
NOT_A_RECEIPT = ("This doesn't look like a receipt: no amounts, date or VAT number were found. "
                 "Ignore it, or type the details in from the photo.")


def looks_like_receipt(text: ReceiptText, rows: list[str]) -> bool:
    """A photo of a desk, a screen or a keyboard still gets a few words out
    of OCR. A receipt has at least one of: a total, an amount with pence, a
    printed date, a VAT number."""
    return bool(text.total is not None or text.candidate_totals or text.date or text.vat_number
                or any(_AMOUNT.search(r) for r in rows))


@dataclass
class InboxReport:
    staged: list[int] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    waiting: int = 0          # still syncing from iCloud

    @property
    def found(self) -> int:
        return len(self.staged)


# ---- finding files -------------------------------------------------------------

def pending_files(inbox: Path) -> list[tuple[Path, str | None]]:
    """Files waiting in the inbox, with the `paid_by` their folder implies.

    iCloud may show a file as a placeholder (".IMG_1.jpg.icloud") until it
    downloads; asking for it starts the download and it's picked up on a
    later pass.
    """
    found: list[tuple[Path, str | None]] = []
    if not inbox.is_dir():
        return found
    folders: list[tuple[Path, str | None]] = [(inbox, None)]
    for child in inbox.iterdir():
        if child.is_dir() and child.name.lower() in PAID_BY:
            folders.append((child, PAID_BY[child.name.lower()]))
    for folder, paid_by in folders:
        for path in sorted(folder.iterdir()):
            if path.is_dir():
                continue
            if path.name.startswith(".") and path.name.endswith(".icloud"):
                _request_download(path)
                continue
            if path.name.startswith("."):
                continue                       # .DS_Store and friends
            found.append((path, paid_by))
    return found


def _request_download(placeholder: Path) -> None:
    real = placeholder.with_name(placeholder.name[1:-len(".icloud")])
    try:
        subprocess.run(["brctl", "download", str(real)], capture_output=True, timeout=30)
    except Exception:
        log.debug("could not ask iCloud for %s", real, exc_info=True)


def _settled(path: Path, now: float) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    return stat.st_size > 0 and now - stat.st_mtime >= SETTLE_SECONDS


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---- reading one photo into a receipt row -------------------------------------------

def _photo_date(reading: PhotoReading, filename: str) -> datetime | None:
    """When the photo was taken: EXIF first, then the Shortcut's filename
    timestamp ("2026-10-06 191502 camera.jpg"). Shared files have none."""
    if reading.photo_taken:
        return reading.photo_taken
    if filename.endswith(" camera.jpg") or filename.endswith(" camera.jpeg"):
        try:
            return datetime.strptime(filename[:17], "%Y-%m-%d %H%M%S")
        except ValueError:
            return None
    return None


def assess(text: ReceiptText, photo_taken: datetime | None, paid_by: str | None,
           supplier_source: str, today: date | None = None) -> tuple[date | None, list[str]]:
    """The date to use, and what a person should check, in plain words.

    Implements the date cross-check (PLAN.md §5.4) and turns every blank or
    doubt from the reader into a flag. Flags never block filing by hand;
    they stop automatic filing and say why.
    """
    today = today or date.today()
    flags: list[str] = []

    # total
    if text.total is None:
        if len(text.candidate_totals) > 1:
            amounts = " or ".join(str(t) for t in text.candidate_totals)
            flags.append(f"Several totals ({amounts}). A split bill? Enter what you paid.")
        else:
            flags.append("Total couldn't be read. Type it in from the photo.")
    elif text.total_note == FROM_NAME:
        flags.extend(total_from_name_flags([])[:1])
    elif text.total_status != CONFIRMED:
        flags.append("Total not confirmed by a payment line. Check it against the photo.")

    # currency
    if text.currency is None:
        flags.append("Dollars. Which currency?" if text.currency_hint == "$"
                     else "No currency shown. Which currency?")

    # date, cross-checked against when the photo was taken
    when = text.date
    taken = photo_taken.date() if photo_taken else None
    if when is None and taken is not None:
        when = taken
        flags.append("No date on the receipt; using the day the photo was taken.")
    elif when is None:
        flags.append("No date found. Enter it from the receipt.")
    elif taken is not None:
        if when > taken:
            # a receipt can't be printed after it was photographed
            swapped = None
            if when.day <= 12:
                try:
                    swapped = date(when.year, when.day, when.month)
                except ValueError:
                    swapped = None
            if swapped and taken - timedelta(days=60) <= swapped <= taken:
                when = swapped
                flags.append("Date corrected: day and month were swapped.")
            else:
                flags.append(f"Receipt date ({when:%-d %b %Y}) is after the photo was taken "
                             f"({taken:%-d %b %Y}). Check the date.")
        elif when < taken - timedelta(days=60):
            flags.append(f"Receipt dated {when:%-d %b %Y} but photographed {taken:%-d %b %Y}. Check the date.")
    if when is not None and when < today - OLDEST_USEFUL:
        flags.append("Older than 18 months.")
    if when is not None and when > today:
        flags.append("Dated in the future. Check the date.")

    # VAT
    if text.not_vat_receipt:
        flags.append("Says it is not a VAT receipt: no VAT can be reclaimed.")
    elif text.vat and not text.vat_number:
        flags.append("VAT shown but no VAT number found. Reclaim anyway?")

    # other
    if text.reprint:
        flags.append("Printed as a duplicate or reprint. Check the original isn't already filed.")
    if paid_by is None:
        flags.append("Paid from the business account, or personally?")
    if supplier_source == "model":
        flags.append("Supplier name guessed by the Mac. Check it.")
    elif supplier_source == "first line":
        flags.append("Supplier name taken from the top of the receipt. Check it.")
    return when, flags


def _supplier(text: ReceiptText, reading: PhotoReading, db: Database) -> tuple[str, str]:
    """(name, where it came from). A name you've confirmed for this VAT
    number wins; then the Mac's guess; then the first line with words. A
    photo that isn't a receipt gets no name: one would only be made up."""
    if not looks_like_receipt(text, reading.rows):
        return UNKNOWN_SUPPLIER, "none"
    if text.vat_number:
        known = db.get_state(f"supplier_by_vat:{text.vat_number}")
        if known:
            return known, "you"
    if reading.supplier_guess:
        return reading.supplier_guess, "model"
    for row in reading.rows:
        words = row.strip()
        if sum(ch.isalpha() for ch in words) >= 3:
            return words[:60], "first line"
    return UNKNOWN_SUPPLIER, "none"


FROM_NAME = "from the supplier name"


def _total_from_name(text: ReceiptText, supplier: str) -> ReceiptText:
    """No total read, but the supplier name carries one price in pounds
    (the Mac names a ticket "Hackney Central to North Greenwich £2.30"):
    that is the total, unconfirmed. A total read from the receipt, or a
    receipt in another currency, is left alone."""
    price = price_in_name(supplier)
    if text.total is not None or text.currency not in (None, "GBP") or price is None:
        return text
    return replace(text, total=price, currency="GBP", currency_hint="",
                   total_status=UNCONFIRMED, total_note=FROM_NAME)


def price_in_name(name: str | None) -> Decimal | None:
    """The one price in pounds a supplier name carries, else None (none,
    or two to choose between)."""
    prices = {m.replace(",", "") for m in _POUNDS_IN_NAME.findall(name or "")}
    return Decimal(prices.pop()) if len(prices) == 1 else None


def total_from_name_flags(flags: list[str]) -> list[str]:
    """A staged photo's flags once its total is taken from its name: the
    total and currency are no longer missing."""
    kept = [f for f in flags if not f.startswith(("Total couldn't be read", "Several totals", "Dollars. Which currency",
                                                   "No currency shown"))]
    return ["Total taken from the £ price in the supplier name. Check it against the photo.", *kept]


def _filename(when: date | None, supplier: str, currency: str | None,
              total, suffix: str) -> str:
    parts = [when.isoformat() if when else "undated", supplier]
    if total is not None:
        parts.append(f"{currency or ''}{total}")
    name = " ".join(parts)
    name = "".join(ch for ch in name if ch not in '/\\:*?"<>|').strip()
    return f"{name}{suffix}"


def receipt_fields(reading: PhotoReading, *, paid_by: str | None, filename: str,
                   db: Database, exclude_id: int | None = None) -> dict:
    """The receipt row for one reading (new photo, or a photo read again)."""
    text = reading.text
    taken = _photo_date(reading, filename)
    supplier, supplier_source = _supplier(text, reading, db)
    text = _total_from_name(text, supplier)
    when, flags = assess(text, taken, paid_by, supplier_source)
    receipt_like = looks_like_receipt(text, reading.rows)
    currency = text.currency
    if not receipt_like:
        # the rest of the flags (no total, no date…) all follow from this one
        flags = [NOT_A_RECEIPT] + [f for f in flags if "personally?" in f]
        currency = None            # a € key on a keyboard isn't a currency

    duplicate_of = None
    if text.total is not None:
        for row in db.with_total(float(text.total), exclude_id=exclude_id):
            other = ReceiptText(
                total=text.total,
                date=date.fromisoformat(row["purchased_on"]) if row["purchased_on"] else None,
                vat_number=row["vat_number"],
                auth_code=_extra(row).get("auth_code"),
            )
            why = same_purchase(text, other)
            if why:
                duplicate_of = row["id"]
                flags.insert(0, f"Looks like the same purchase as {row['vendor'] or 'receipt'} "
                                f"#{row['id']} ({why}).")
                break

    return {
        "vendor": supplier,
        "purchased_on": when.isoformat() if when else None,
        "total": float(text.total) if text.total is not None else None,
        "currency": currency,
        "vat": float(text.vat) if text.vat is not None else None,
        "vat_number": text.vat_number,
        "total_status": text.total_status,
        "photo_taken": taken.isoformat(timespec="seconds") if taken else None,
        "paid_by": paid_by,
        "description": "Expense" if paid_by == "personal" else "Photo receipt",
        "extra_json": {
            "flags": flags,
            "supplier_source": supplier_source,
            "not_a_receipt": not receipt_like,
            "total_note": text.total_note,
            "currency_hint": text.currency_hint,
            "candidate_totals": [str(t) for t in text.candidate_totals],
            "other_dates": [d.isoformat() for d in text.other_dates],
            "auth_code": text.auth_code,
            "vat_lines": [[None if r is None else str(r), str(v), None if g is None else str(g)]
                          for r, v, g in text.vat_lines],
            "duplicate_of": duplicate_of,
            "skew_degrees": reading.skew_degrees,
            "latitude": reading.latitude,
            "longitude": reading.longitude,
            "model": reading.model,
            "rows": reading.rows,
            "layout": reading.layout,
        },
    }


def tidy(reading: PhotoReading) -> tuple[PhotoReading, dict | None]:
    """The reading to use (the tidied copy's when one lost nothing), and
    what to keep in extra_json about it, so review can switch back to the
    plain copy. Copies not chosen are deleted."""
    if reading.jpeg is None or not (reading.clean or reading.clean_note):
        return reading, None
    copy, notes = choose_copy(reading)
    for other in reading.clean:
        if other is not copy:
            other.path.unlink(missing_ok=True)
    record = {
        "used": copy is not None,
        "kind": copy.kind if copy else None,
        "steps": copy.steps if copy else [],
        "dropped": copy.dropped if copy else [],
        "notes": notes,
        "plain_path": str(reading.jpeg),
        "plain_layout": reading.layout,
        "tidied_path": str(copy.path) if copy else None,
        "tidied_layout": copy.layout if copy else None,
    }
    return (using_copy(reading, copy) if copy else reading), record


def _with_tidy(fields: dict, record: dict | None) -> dict:
    if record is not None:
        fields["extra_json"]["tidy"] = record
    return fields


def _extra(row) -> dict:
    import json
    try:
        return json.loads(row["extra_json"] or "{}")
    except (TypeError, ValueError):
        return {}


# ---- the inbox pass ---------------------------------------------------------------

Reader = Callable[..., PhotoReading]


def process_inbox(config: Config, db: Database, *, reader: Reader = read_file,
                  note: Callable[[str], None] = lambda line: None,
                  now: float | None = None, inbox: Path | None = None,
                  archive_dir: Path | None = None) -> InboxReport:
    """Stage every settled file in the inbox. Safe to run often.

    `inbox` and `archive_dir` are the user's choices from Settings (any
    folder: iCloud Drive, Dropbox, Google Drive's desktop folder…);
    without them, the config defaults."""
    report = InboxReport()
    inbox = inbox or config.photo_inbox
    archive_dir = archive_dir or config.photos_dir
    now = time.time() if now is None else now
    for path, paid_by in pending_files(inbox):
        if not _settled(path, now):
            report.waiting += 1
            continue
        suffix = path.suffix.lower()
        if suffix not in READABLE:
            # Out of iCloud too, but kept: it may be something you meant to send.
            rejected = config.data_dir / "rejected" / path.name
            rejected.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(rejected))
            report.rejected.append(path.name)
            note(f"Not a photo or PDF, moved to data/rejected: {path.name}")
            continue

        digest = _sha256(path)
        source_id = f"sha256:{digest}"
        if db.has_source(source_id):
            path.unlink()                    # byte-identical to one already staged
            report.duplicates.append(path.name)
            note(f"Already have this exact photo: {path.name}")
            continue

        # Archive the original before anything else touches it, and prove the
        # copy is byte-identical: the inbox file is deleted further down.
        # Named so a person browsing the archive can tell what's what.
        stamp = datetime.now()
        archive = archive_dir / f"{stamp:%Y-%m}" / f"{path.stem} {digest[:8]}{suffix}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, archive)
        if _sha256(archive) != digest:
            archive.unlink(missing_ok=True)
            report.failed.append(path.name)
            note(f"Copy of {path.name} didn't match the original; left it in iCloud")
            continue

        jpeg = None if suffix == ".pdf" else config.pdf_dir / f"photo-{digest[:16]}.jpg"
        tidied = None if jpeg is None else jpeg.with_suffix("")
        row = {
            "watcher_id": WATCHER_ID,
            "source": "photo",
            "source_id": source_id,
            "original_path": str(archive),
            "subject": path.name,
            "pdf_source": "photo",
        }
        try:
            reading = reader(archive, config.data_dir, jpeg_out=jpeg, supplier=True, clean_prefix=tidied)
        except ReaderError as exc:
            row.update({"status": FAILED, "error": str(exc), "paid_by": paid_by,
                        "vendor": "Photo receipt", "pdf_path": str(archive),
                        "filename": path.name})
            receipt_id = db.insert_receipt(row)
            report.failed.append(path.name)
            note(f"Couldn't read {path.name}: {exc}")
        else:
            reading, record = tidy(reading)
            fields = _with_tidy(receipt_fields(reading, paid_by=paid_by, filename=path.name, db=db), record)
            document = reading.jpeg or archive
            row.update(fields)
            row.update({
                "status": PENDING,
                "pdf_path": str(document),
                "filename": _filename(
                    date.fromisoformat(fields["purchased_on"]) if fields["purchased_on"] else None,
                    fields["vendor"], fields["currency"], reading.text.total, document.suffix.lower()),
            })
            receipt_id = db.insert_receipt(row)
            if receipt_id:
                report.staged.append(receipt_id)
                total = f"{fields['currency'] or ''} {reading.text.total}" if reading.text.total else "total unread"
                note(f"Photo: {fields['vendor']}, {total}")
        # Staged (or recorded as failed) and archived: leave nothing in iCloud.
        path.unlink(missing_ok=True)
    return report


def reread(config: Config, db: Database, receipt_id: int, *, reader: Reader = read_file) -> bool:
    """Read a staged photo again from its archived original."""
    row = db.get_receipt(receipt_id)
    if row is None or row["source"] != "photo" or not row["original_path"]:
        return False
    original = Path(row["original_path"])
    if not original.exists():
        db.update_receipt(receipt_id, {"status": FAILED, "error": "the original photo is missing"})
        return False
    jpeg = None if original.suffix.lower() == ".pdf" else config.pdf_dir / f"photo-{original.stem}.jpg"
    tidied = None if jpeg is None else jpeg.with_suffix("")
    try:
        reading = reader(original, config.data_dir, jpeg_out=jpeg, supplier=True, clean_prefix=tidied)
    except ReaderError as exc:
        db.update_receipt(receipt_id, {"status": FAILED, "error": str(exc)})
        return False
    reading, record = tidy(reading)
    fields = _with_tidy(receipt_fields(reading, paid_by=row["paid_by"], filename=row["subject"] or "",
                                       db=db, exclude_id=receipt_id), record)
    document = reading.jpeg or original
    fields.update({"status": PENDING, "error": None, "pdf_path": str(document)})
    db.update_receipt(receipt_id, fields)
    return True
