"""Export approved receipts as a batch ready for FreeAgent.

FreeAgent's Smart Capture has no public API — it is an upload/mobile-photo
feature, and API access to it remains an open request on their developer
forum. So the last hop stays manual: this writes a dated folder of correctly
named PDFs plus a manifest, and you drag that folder into Smart Capture.

Everything upstream of that drag is automated, and the folder is exactly what
a bookkeeper would want if you ever hand the job over.
"""

from __future__ import annotations

import csv
import logging
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

MANIFEST_COLUMNS = [
    "date_of_payment",
    "supplier",
    "currency",
    "amount",
    "reference",
    "description",
    "pdf_source",
    "filename",
]


@dataclass
class ExportResult:
    folder: Path
    exported: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.exported)


def _unique_path(folder: Path, filename: str) -> Path:
    """Avoid clobbering when two receipts render the same name."""
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 100):
        alternative = folder / f"{stem} ({index}){suffix}"
        if not alternative.exists():
            return alternative
    raise RuntimeError(f"could not find a free filename for {filename}")


def export_receipts(
    config: Config,
    db: Database,
    receipt_ids: list[int],
    batch_name: str | None = None,
) -> ExportResult:
    """Copy the chosen receipts into a dated batch folder."""
    folder = config.export_dir / (batch_name or date.today().isoformat())
    counter = 2
    while folder.exists() and any(folder.iterdir()):
        folder = config.export_dir / (
            f"{batch_name or date.today().isoformat()}-{counter}"
        )
        counter += 1
    folder.mkdir(parents=True, exist_ok=True)

    result = ExportResult(folder=folder)
    rows: list[dict[str, object]] = []

    for receipt_id in receipt_ids:
        record = db.get_receipt(receipt_id)
        if record is None:
            result.missing.append(f"receipt {receipt_id} no longer exists")
            continue

        source = Path(record["pdf_path"]) if record["pdf_path"] else None
        if source is None or not source.exists():
            result.missing.append(
                f"{record['filename'] or receipt_id}: PDF missing from staging"
            )
            continue

        destination = _unique_path(
            folder, record["filename"] or f"receipt-{receipt_id}.pdf"
        )
        shutil.copy2(source, destination)
        db.mark_exported(receipt_id, str(destination))
        result.exported.append(destination.name)

        rows.append(
            {
                "date_of_payment": record["purchased_on"] or "",
                "supplier": record["vendor"] or "",
                "currency": record["currency"] or "",
                "amount": (
                    f"{record['total']:.2f}" if record["total"] is not None else ""
                ),
                "reference": record["reference"] or "",
                "description": record["description"] or "",
                "pdf_source": record["pdf_source"] or "",
                "filename": destination.name,
            }
        )

    if rows:
        manifest = folder / "manifest.csv"
        with manifest.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    return result


def reveal(path: Path) -> bool:
    """Open the batch folder in the desktop file manager, best effort."""
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.run(["open", str(path)], check=False)
        elif system == "Windows":
            subprocess.run(["explorer", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
        return True
    except Exception:
        log.debug("could not open %s in the file manager", path, exc_info=True)
        return False
