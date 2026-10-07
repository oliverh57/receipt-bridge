"""Read a receipt photo or PDF on this Mac: OCR by the `receipt-reader`
Swift helper, then money, VAT and dates by app/receipt_text.py.

The helper is compiled on first use into data/bin/ and rebuilt whenever
native/receipt-reader.swift changes, the same way the rest of the app runs
this folder's code without rebuilding the .app.

Photos can also be tidied (cropped to the receipt, grey, contrast lifted).
The helper offers up to two copies and reads each again; `choose_copy`
only takes one whose reading lost nothing the original's had.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import ROOT
from .receipt_text import CONFIRMED, ReceiptText, read_rows

log = logging.getLogger(__name__)

SOURCE = ROOT / "native" / "receipt-reader.swift"
TIMEOUT_SECONDS = 120
_build_lock = threading.Lock()


class ReaderError(RuntimeError):
    """The helper couldn't be built or couldn't read the file."""


@dataclass
class CleanCopy:
    """A tidied copy of a photo, with what was read from it."""
    kind: str                     # "crop" (cut to the receipt) or "enhance" (nothing cut away)
    path: Path
    steps: list[str]              # what was done, in plain words
    rows: list[str]
    layout: list
    dropped: list[str] = field(default_factory=list)  # background text the crop cut away


@dataclass
class PhotoReading:
    text: ReceiptText
    rows: list[str]
    photo_taken: datetime | None = None    # EXIF capture time, local
    latitude: float | None = None
    longitude: float | None = None
    skew_degrees: float = 0.0
    jpeg: Path | None = None                # FreeAgent-sized copy, if asked for
    supplier_guess: str | None = None       # on-device model: names only, never money
    model: str = "not asked"
    extra: dict = field(default_factory=dict)
    layout: list = field(default_factory=list)  # each row's pieces and where they sit (app/highlights.py)
    page_images: list = field(default_factory=list)  # a PDF's pages as JPEGs, when asked for
    clean: list[CleanCopy] = field(default_factory=list)  # tidied copies, crop first
    clean_note: str | None = None                          # why no crop was offered


def helper_path(data_dir: Path) -> Path:
    return data_dir / "bin" / "receipt-reader"


def ensure_built(data_dir: Path) -> Path:
    """Compile the helper if it's missing or older than its source."""
    binary = helper_path(data_dir)
    with _build_lock:
        if binary.exists() and binary.stat().st_mtime >= SOURCE.stat().st_mtime:
            return binary
        binary.parent.mkdir(parents=True, exist_ok=True)
        log.info("compiling %s", SOURCE.name)
        try:
            result = subprocess.run(
                ["swiftc", "-O", str(SOURCE), "-o", str(binary)],
                capture_output=True, text=True, timeout=600,
            )
        except FileNotFoundError as exc:
            raise ReaderError(
                "swiftc not found: install Xcode or its Command Line Tools "
                "(xcode-select --install)"
            ) from exc
        if result.returncode != 0:
            raise ReaderError(f"receipt-reader did not compile: {result.stderr.strip()[:500]}")
        return binary


def _parse_taken(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def read_file(
    path: Path,
    data_dir: Path,
    *,
    jpeg_out: Path | None = None,
    supplier: bool = False,
    pages_out: Path | None = None,
    clean_prefix: Path | None = None,
) -> PhotoReading:
    """OCR `path` (photo or PDF) and read it. Raises ReaderError.

    `clean_prefix` (photos): also write tidied copies as
    <prefix>.crop.jpg / <prefix>.enhance.jpg, each read again."""
    binary = ensure_built(data_dir)
    command = [str(binary), str(path)]
    if jpeg_out is not None:
        jpeg_out.parent.mkdir(parents=True, exist_ok=True)
        command += ["--jpeg", str(jpeg_out)]
    if supplier:
        command.append("--supplier")
    if pages_out is not None:
        command += ["--pages", str(pages_out)]
    if clean_prefix is not None:
        clean_prefix.parent.mkdir(parents=True, exist_ok=True)
        command += ["--clean", str(clean_prefix)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ReaderError(f"reading {path.name} took over {TIMEOUT_SECONDS}s") from exc
    if result.returncode != 0:
        raise ReaderError(f"could not read {path.name}: {result.stderr.strip()[:300]}")
    data = json.loads(result.stdout)
    rows = data.get("rows") or []
    return PhotoReading(
        text=read_rows(rows),
        rows=rows,
        photo_taken=_parse_taken(data.get("photo_taken")),
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
        skew_degrees=data.get("skew_degrees") or 0.0,
        jpeg=Path(data["jpeg"]) if data.get("jpeg") else None,
        supplier_guess=(data.get("supplier_guess") or "").strip() or None,
        model=data.get("model", "not asked"),
        layout=data.get("layout") or [],
        page_images=[Path(p) for p in data.get("page_images") or []],
        clean=[CleanCopy(kind=c["kind"], path=Path(c["path"]), steps=c.get("steps") or [],
                         rows=c.get("rows") or [], layout=c.get("layout") or [],
                         dropped=c.get("dropped") or [])
               for c in data.get("clean") or []],
        clean_note=data.get("clean_note"),
    )


# ---- choosing a tidied copy ---------------------------------------------------------

_AMOUNT = re.compile(r"\d[.,]\d{2}(?!\d)")
_BETWEEN_DIGITS = re.compile(r"(?<=\d)[\s.,:·]+(?=\d)")


def _amounts(rows: list[str]) -> list[str]:
    """Every printed amount, as its digits ("£11.40" → "1140")."""
    return [re.sub(r"\D", "", a) for row in rows for a in _AMOUNT.findall(row)]


def _digit_runs(rows: list[str]) -> list[str]:
    """Each row's numbers with the gaps and points between digits taken
    out, so "4.40", "4 40" and "4:40" all contain "440"."""
    return [run for row in rows for run in re.findall(r"\d+", _BETWEEN_DIGITS.sub("", row))]


def lost(original: ReceiptText, original_rows: list[str], copy: ReceiptText, copy_rows: list[str]) -> list[str]:
    """What the original's reading had that the copy's doesn't, in plain
    words. Empty means nothing was lost: every value read from the original
    reads the same from the copy, and every printed amount's digits are
    still there.
    A value the copy reads that the original didn't is a gain, not a loss."""
    out: list[str] = []
    for name, label in (("total", "total"), ("vat", "VAT"), ("vat_number", "VAT number"),
                        ("date", "date"), ("currency", "currency"), ("auth_code", "auth code")):
        was = getattr(original, name)
        if was is not None and getattr(copy, name) != was:
            out.append(label)
    if original.total_status == CONFIRMED and copy.total_status != CONFIRMED:
        out.append("total's payment line")
    if not set(original.other_dates) <= set(copy.other_dates) | {copy.date}:
        out.append("a date")
    # Every amount's digits must still be there. OCR is allowed to read the
    # point differently ("4.40" as "4 40"); it isn't allowed a different digit.
    pool = _digit_runs(copy_rows)
    missing = 0
    for amount in _amounts(original_rows):
        for i, run in enumerate(pool):
            if amount in run:
                pool[i] = run.replace(amount, "|", 1)
                break
        else:
            missing += 1
    if missing:
        out.append(f"{missing} amount{'s' if missing > 1 else ''}")
    return out


def choose_copy(reading: PhotoReading) -> tuple[CleanCopy | None, list[str]]:
    """The first tidied copy (crop, then enhance-only) that lost nothing,
    and notes on any that were turned down and why."""
    notes: list[str] = []
    if reading.clean_note:
        notes.append(f"Not cropped: {reading.clean_note}.")
    for copy in reading.clean:
        missing = lost(reading.text, reading.rows, read_rows(copy.rows), copy.rows)
        if not missing:
            return copy, notes
        what = "Cropped copy" if copy.kind == "crop" else "Contrast-only copy"
        notes.append(f"{what} not used: {', '.join(missing)} didn't read the same.")
    return None, notes


def using_copy(reading: PhotoReading, copy: CleanCopy) -> PhotoReading:
    """The reading as if the tidied copy were the photo: its text, and its
    layout so highlights land on the image shown."""
    from dataclasses import replace
    return replace(reading, text=read_rows(copy.rows), rows=copy.rows, layout=copy.layout, jpeg=copy.path)
