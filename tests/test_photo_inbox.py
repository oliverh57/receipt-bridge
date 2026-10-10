"""Photo inbox: files dropped by the iPhone Shortcut into iCloud, staged in
To file. Uses real receipt photos and the real on-device reader, in a
throwaway inbox and data folder. No network.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.db import FAILED, PENDING, Database  # noqa: E402
from app.photo_inbox import assess, process_inbox, reread  # noqa: E402
from app.receipt_reader import ReaderError, read_file  # noqa: E402
from app.receipt_text import CONFIRMED, ReceiptText  # noqa: E402

PHOTOS = Path(__file__).parent / "fixtures" / "photos"
LATER = time.time() + 3600      # every file has "settled"


def real_reader(path, _data_dir, **kwargs):
    # Build and run the helper from the project's data/, not the temp one.
    return read_file(path, ROOT / "data", **kwargs)


def make_env() -> tuple[Config, Database, Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    config = Config(raw={"data_dir": str(base / "data"), "photo_inbox": str(base / "inbox"),
                         "export_dir": str(base / "export")})
    config.ensure_dirs()
    for sub in ("Bank", "Expense"):
        (config.photo_inbox / sub).mkdir(parents=True)
    return config, Database(config.db_path), config.photo_inbox, tmp


def drop(inbox: Path, fixture: str, folder: str = "", name: str | None = None) -> Path:
    target = inbox / folder / (name or fixture)
    shutil.copy(PHOTOS / fixture, target)
    return target


def extra(db: Database, receipt_id: int) -> dict:
    return json.loads(db.get_receipt(receipt_id)["extra_json"])


def test_a_bank_photo_is_read_staged_archived_and_removed_from_icloud() -> None:
    config, db, inbox, tmp = make_env()
    with tmp:
        dropped = drop(inbox, "sandwi-bristol.webp", "Bank")
        report = process_inbox(config, db, reader=real_reader, now=LATER)
        assert len(report.staged) == 1, report
        row = db.get_receipt(report.staged[0])
        assert row["status"] == PENDING and row["source"] == "photo"
        assert row["paid_by"] == "business"
        assert row["total"] == 7.05 and row["currency"] == "GBP"
        assert row["vat"] == 0.52 and row["vat_number"] == "GB328153019"
        assert row["purchased_on"] == "2025-06-25"
        assert row["total_status"] == CONFIRMED
        assert not dropped.exists(), "left in iCloud"
        assert Path(row["original_path"]).exists(), "original not archived"
        assert Path(row["pdf_path"]).suffix == ".jpg" and Path(row["pdf_path"]).exists()



def test_a_clear_photo_is_tidied_and_the_original_kept() -> None:
    """Greggs crops cleanly: the copy shown (and filed) is the tidied one,
    the highlights follow it, and the plain copy and untouched original are
    both kept so review can switch back."""
    config, db, inbox, tmp = make_env()
    with tmp:
        original_bytes = (PHOTOS / "greggs.webp").read_bytes()
        drop(inbox, "greggs.webp", "Bank")
        report = process_inbox(config, db, reader=real_reader, now=LATER)
        row = db.get_receipt(report.staged[0])
        tidy = extra(db, row["id"])["tidy"]
        assert tidy["used"] and tidy["kind"] == "crop", tidy["notes"]
        assert row["pdf_path"] == tidy["tidied_path"] and row["pdf_path"].endswith(".crop.jpg")
        assert extra(db, row["id"])["layout"] == tidy["tidied_layout"]
        assert Path(tidy["plain_path"]).exists()
        assert not Path(tidy["plain_path"]).with_suffix(".enhance.jpg").exists(), "unused copy left behind"
        assert Path(row["original_path"]).read_bytes() == original_bytes

def test_the_folder_says_who_paid() -> None:
    config, db, inbox, tmp = make_env()
    with tmp:
        drop(inbox, "greggs.webp", "Expense")
        drop(inbox, "tesco-highbury.webp")              # loose: not asked
        process_inbox(config, db, reader=real_reader, now=LATER)
        rows = {r["vendor"]: r for r in db.list_receipts(PENDING)}
        paid = {r["paid_by"] for r in rows.values()}
        assert paid == {"personal", None}, paid
        loose = next(r for r in rows.values() if r["paid_by"] is None)
        assert any("personally" in f for f in extra(db, loose["id"])["flags"])


def test_the_same_photo_twice_is_staged_once() -> None:
    config, db, inbox, tmp = make_env()
    with tmp:
        drop(inbox, "golden-goose-duplicate.webp", "Bank")
        process_inbox(config, db, reader=real_reader, now=LATER)
        again = drop(inbox, "golden-goose-duplicate.webp", "Bank", name="again.webp")
        report = process_inbox(config, db, reader=real_reader, now=LATER)
        assert report.duplicates == ["again.webp"] and not report.staged
        assert not again.exists()
        assert len(db.list_receipts()) == 1


def test_the_same_purchase_photographed_twice_is_flagged() -> None:
    config, db, inbox, tmp = make_env()
    with tmp:
        drop(inbox, "sainsburys-whitstable-1.webp", "Bank")
        process_inbox(config, db, reader=real_reader, now=LATER)
        drop(inbox, "sainsburys-whitstable-1-full.webp", "Bank")
        report = process_inbox(config, db, reader=real_reader, now=LATER)
        second = extra(db, report.staged[0])
        assert second["duplicate_of"], "same £4.49 and auth code 228556"
        assert "same purchase" in second["flags"][0]


def test_a_file_still_syncing_is_left_alone() -> None:
    config, db, inbox, tmp = make_env()
    with tmp:
        dropped = drop(inbox, "greggs.webp", "Bank")
        report = process_inbox(config, db, reader=real_reader, now=time.time())
        assert report.waiting == 1 and dropped.exists() and not db.list_receipts()


def test_other_files_are_moved_to_rejected() -> None:
    config, db, inbox, tmp = make_env()
    with tmp:
        (inbox / "Bank" / "notes.txt").write_text("hello")
        report = process_inbox(config, db, reader=real_reader, now=LATER)
        assert report.rejected == ["notes.txt"]
        assert (config.data_dir / "rejected" / "notes.txt").exists()
        assert not (inbox / "Bank" / "notes.txt").exists(), "left in iCloud"


def test_an_unreadable_photo_is_kept_and_shown_as_failed() -> None:
    def broken(*_args, **_kwargs):
        raise ReaderError("cannot open image")

    config, db, inbox, tmp = make_env()
    with tmp:
        drop(inbox, "greggs.webp", "Bank")
        report = process_inbox(config, db, reader=broken, now=LATER)
        assert report.failed == ["greggs.webp"]
        row = db.list_receipts(FAILED)[0]
        assert "cannot open image" in row["error"]
        assert Path(row["original_path"]).exists(), "original must survive a failed read"

        assert reread(config, db, row["id"], reader=real_reader)
        again = db.get_receipt(row["id"])
        assert again["status"] == PENDING and again["total"] == 3.40


def test_the_rules_find_split_bills_and_unknown_currencies() -> None:
    config, db, inbox, tmp = make_env()
    with tmp:
        drop(inbox, "christiani-split-bill.webp", "Expense")
        drop(inbox, "momiji-dc.webp", "Expense")
        process_inbox(config, db, reader=real_reader, now=LATER)
        flags = [f for r in db.list_receipts(PENDING) for f in extra(db, r["id"])["flags"]]
        assert any("split bill" in f for f in flags), flags
        assert any("Which currency" in f for f in flags), flags



# A photo of a laptop: OCR picked up a word on the screen and the € key.
NOT_A_RECEIPT_ROWS = ["Explode", "•", "Pg", "80", "7", "2 €   €", "3#", "•   Q", "W", "E", "S", "D", "1",
                      "control", "(", "option", "command"]


def test_a_photo_that_is_not_a_receipt_gets_no_made_up_name_or_currency() -> None:
    from app.photo_inbox import looks_like_receipt, receipt_fields
    from app.receipt_reader import PhotoReading
    from app.receipt_text import read_rows
    config, db, _inbox, tmp = make_env()
    with tmp:
        text = read_rows(NOT_A_RECEIPT_ROWS)
        assert not looks_like_receipt(text, NOT_A_RECEIPT_ROWS)
        fields = receipt_fields(PhotoReading(text=text, rows=NOT_A_RECEIPT_ROWS), paid_by="business",
                                filename="IMG_1.jpg", db=db)
        assert fields["vendor"] == "Unknown supplier"
        assert fields["currency"] is None
        assert fields["extra_json"]["not_a_receipt"] is True
        assert fields["extra_json"]["flags"][0].startswith("This doesn't look like a receipt")
        assert len(fields["extra_json"]["flags"]) == 1


def test_a_price_in_the_supplier_name_is_the_total_when_none_was_read() -> None:
    from app.photo_inbox import receipt_fields
    from app.receipt_reader import PhotoReading
    from app.receipt_text import UNCONFIRMED, read_rows
    rows = ["Journey history", "Wed 8 October 2026", "Hackney Central to North Greenwich", "07:42 - 08:15"]
    config, db, _inbox, tmp = make_env()
    with tmp:
        def fields(name: str, given: list[str] = rows) -> dict:
            return receipt_fields(PhotoReading(text=read_rows(given), rows=given, supplier_guess=name),
                                  paid_by="business", filename="IMG_1.jpg", db=db)
        assert read_rows(rows).total is None
        got = fields("Hackney Central to North Greenwich £2.30")
        assert (got["total"], got["currency"], got["total_status"]) == (2.30, "GBP", UNCONFIRMED), got
        assert "Total taken from the £ price in the supplier name. Check it against the photo." in got["extra_json"]["flags"]
        assert not any("couldn't be read" in f for f in got["extra_json"]["flags"])
        assert fields("Hackney Central")["total"] is None, "no price in the name"
        assert fields("Return £2.30 or £4.60")["total"] is None, "two prices: which one?"
        paid = rows + ["TOTAL £3.10", "VISA £3.10"]
        assert fields("Hackney Central to North Greenwich £2.30", paid)["total"] == 3.10, "a total read wins"


def test_photos_already_in_files_get_the_price_in_their_name_as_their_total() -> None:
    from app.service import ReceiptService
    config, db, _inbox, tmp = make_env()
    with tmp:
        def photo(vendor: str, **more) -> int:
            return db.insert_receipt({"watcher_id": "photo", "source": "photo", "vendor": vendor,
                                      "created_at": "2026-10-08T08:00:00+00:00", "total_status": "missing",
                                      "extra_json": json.dumps({"flags": ["Total couldn't be read. Type it in from the photo.",
                                                                          "No currency shown. Which currency?",
                                                                          "Supplier name guessed by the Mac. Check it."]}),
                                      **more})
        ticket = photo("Hackney Central to North Greenwich £2.30")
        typed = photo("Hackney Central to Dollis Hill £2.50", total=2.6, currency="GBP")
        plain = photo("Hackney Central")
        service = ReceiptService(config)
        assert service.fill_totals_from_names() == 1
        row = db.get_receipt(ticket)
        assert (row["total"], row["currency"], row["total_status"]) == (2.3, "GBP", "unconfirmed")
        assert extra(db, ticket)["flags"] == ["Total taken from the £ price in the supplier name. Check it against the photo.",
                                              "Supplier name guessed by the Mac. Check it."]
        assert db.get_receipt(typed)["total"] == 2.6, "a total already there stays"
        assert db.get_receipt(plain)["total"] is None
        assert service.fill_totals_from_names() == 0, "once"


def test_every_real_receipt_looks_like_one() -> None:
    from app.photo_inbox import looks_like_receipt
    from app.receipt_text import read_rows
    for path in sorted((PHOTOS / "ocr").glob("*.txt")):
        rows = path.read_text().splitlines()
        assert looks_like_receipt(read_rows(rows), rows), path.name


# ---- folders chosen in Settings ---------------------------------------------------

def test_a_chosen_inbox_and_archive_are_used() -> None:
    """Someone using Dropbox instead of iCloud: any folder works."""
    config, db, _inbox, tmp = make_env()
    with tmp:
        base = Path(tmp.name)
        dropbox = base / "Dropbox" / "Receipts"
        (dropbox / "Bank").mkdir(parents=True)
        archive = base / "Documents" / "Receipt originals"
        drop(dropbox, "greggs.webp", "Bank")
        report = process_inbox(config, db, reader=real_reader, now=LATER, inbox=dropbox, archive_dir=archive)
        row = db.get_receipt(report.staged[0])
        original = Path(row["original_path"])
        assert archive in original.parents, original
        assert original.name.startswith("greggs "), "named after the file it came from"
        assert not (dropbox / "Bank" / "greggs.webp").exists()


def test_folder_settings_are_validated() -> None:
    from app.service import ReceiptService
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        config = Config(raw={"data_dir": str(base / "data"), "export_dir": str(base / "export"),
                             "photo_inbox": str(base / "default-inbox"),
                             "freeagent": {"credentials_file": str(base / "none.json")}})
        service = ReceiptService(config)
        inbox = base / "Dropbox" / "Receipts"
        inbox.mkdir(parents=True)
        for bad in ({"inbox": str(base / "missing")},                        # no such folder
                    {"inbox": str(inbox), "archive": str(inbox / "Read")},      # archive inside inbox
                    {"inbox": str(inbox), "archive": str(inbox)}):              # the same folder
            try:
                service.set_folders(**bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted {bad}")
        service.set_folders(inbox=str(inbox), archive=str(base / "Originals"))
        assert service.photo_inbox == inbox.resolve()
        assert service.photo_archive == (base / "Originals").resolve() and service.photo_archive.is_dir()
        snap = service.snapshot()["photo_inbox"]
        assert not snap["is_default"] and not snap["has_subfolders"]
        service.create_inbox_folders()
        assert service.snapshot()["photo_inbox"]["has_subfolders"]
        (inbox / "Bank" / "stuck.jpg").write_bytes(b"x" * 2048)
        assert service.inbox_size() == 2048, "the warning counts what's waiting in the inbox"
        service.reset_folder("inbox")
        assert service.photo_inbox == config.photo_inbox


# ---- the date cross-check (PLAN.md §5.4) -------------------------------------------

def text(**kwargs) -> ReceiptText:
    base = dict(total=Decimal("5.00"), total_status=CONFIRMED, currency="GBP")
    base.update(kwargs)
    return ReceiptText(**base)


def test_no_date_on_the_receipt_uses_the_photo_date() -> None:
    when, flags = assess(text(), datetime(2026, 9, 3, 12, 0), "business", "you", date(2026, 10, 6))
    assert when == date(2026, 9, 3) and any("photo was taken" in f for f in flags)


def test_a_date_after_the_photo_is_swapped_when_that_fits() -> None:
    # 9 March read as 3 September, photographed 10 March
    when, flags = assess(text(date=date(2026, 9, 3)), datetime(2026, 3, 10, 9, 0),
                         "business", "you", date(2026, 10, 6))
    assert when == date(2026, 3, 9) and any("swapped" in f for f in flags)


def test_a_date_after_the_photo_that_cannot_be_fixed_is_flagged() -> None:
    when, flags = assess(text(date=date(2026, 9, 18)), datetime(2026, 9, 3, 9, 0),
                         "business", "you", date(2026, 10, 6))
    assert when == date(2026, 9, 18) and any("after the photo" in f for f in flags)


def test_a_late_night_purchase_photographed_after_midnight_is_fine() -> None:
    _, flags = assess(text(date=date(2026, 9, 2)), datetime(2026, 9, 3, 0, 20),
                      "business", "you", date(2026, 10, 6))
    assert not flags, flags


def test_old_receipts_are_flagged() -> None:
    _, flags = assess(text(date=date(2024, 11, 14)), None, "personal", "you", date(2026, 10, 6))
    assert any("18 months" in f for f in flags)


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
