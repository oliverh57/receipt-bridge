"""Export batching: filenames, manifest, statuses, collisions.

Runs entirely on temp directories with stub PDFs — no browser, no network.
"""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.db import EXPORTED, PENDING, Database  # noqa: E402
from app.export import export_receipts  # noqa: E402


def make_env() -> tuple[Config, Database]:
    root = Path(tempfile.mkdtemp())
    config = Config(
        raw={
            "data_dir": str(root / "data"),
            "export_dir": str(root / "export"),
        }
    )
    config.ensure_dirs()
    return config, Database(config.db_path)


def stage(config: Config, db: Database, n: int, filename: str, total: float) -> int:
    pdf = config.pdf_dir / f"msg{n}.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    return db.insert_receipt(
        {
            "watcher_id": "trainline",
            "gmail_message_id": f"msg{n}",
            "vendor": "Trainline",
            "reference": f"00985565373{n}",
            "purchased_on": "2026-09-03",
            "total": total,
            "currency": "GBP",
            "description": "Train travel, Tottenham Hale to Cambridge North",
            "pdf_path": str(pdf),
            "pdf_source": "trainline_expense_receipt",
            "filename": filename,
            "status": PENDING,
        }
    )


def test_exports_files_and_marks_them_done() -> None:
    config, db = make_env()
    first = stage(config, db, 1, "2026-09-03 Trainline GBP19.95 A to B 1.pdf", 19.95)
    second = stage(config, db, 2, "2026-09-05 Trainline GBP42.60 C to D 2.pdf", 42.60)

    result = export_receipts(config, db, [first, second], batch_name="batch")

    assert result.count == 2, result.missing
    assert not result.missing
    exported = sorted(p.name for p in result.folder.glob("*.pdf"))
    assert exported == [
        "2026-09-03 Trainline GBP19.95 A to B 1.pdf",
        "2026-09-05 Trainline GBP42.60 C to D 2.pdf",
    ]
    assert db.get_receipt(first)["status"] == EXPORTED
    # Exported receipts leave the review queue.
    assert db.list_receipts(PENDING) == []


def test_writes_a_manifest() -> None:
    config, db = make_env()
    receipt = stage(config, db, 1, "receipt.pdf", 19.95)
    result = export_receipts(config, db, [receipt], batch_name="batch")

    rows = list(csv.DictReader((result.folder / "manifest.csv").open()))
    assert len(rows) == 1
    assert rows[0]["amount"] == "19.95"
    assert rows[0]["currency"] == "GBP"
    assert rows[0]["date_of_payment"] == "2026-09-03"
    assert rows[0]["reference"] == "009855653731"
    assert rows[0]["filename"] == "receipt.pdf"


def test_two_receipts_with_the_same_name_do_not_clobber() -> None:
    config, db = make_env()
    a = stage(config, db, 1, "same.pdf", 10.0)
    b = stage(config, db, 2, "same.pdf", 20.0)

    result = export_receipts(config, db, [a, b], batch_name="batch")

    assert result.count == 2
    names = sorted(p.name for p in result.folder.glob("*.pdf"))
    assert names == ["same (2).pdf", "same.pdf"]


def test_missing_pdf_is_reported_not_silently_dropped() -> None:
    config, db = make_env()
    receipt = stage(config, db, 1, "gone.pdf", 10.0)
    Path(db.get_receipt(receipt)["pdf_path"]).unlink()

    result = export_receipts(config, db, [receipt], batch_name="batch")

    assert result.count == 0
    assert len(result.missing) == 1
    # It stays pending so it can be re-fetched rather than being lost.
    assert db.get_receipt(receipt)["status"] == PENDING


def test_a_second_export_gets_its_own_folder() -> None:
    config, db = make_env()
    first = stage(config, db, 1, "a.pdf", 10.0)
    second = stage(config, db, 2, "b.pdf", 20.0)

    one = export_receipts(config, db, [first], batch_name="2026-09-07")
    two = export_receipts(config, db, [second], batch_name="2026-09-07")

    assert one.folder != two.folder
    assert (one.folder / "a.pdf").exists()
    assert (two.folder / "b.pdf").exists()


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print("\nAll tests passed." if not failures else f"\n{failures} test(s) failed.")
    sys.exit(1 if failures else 0)
