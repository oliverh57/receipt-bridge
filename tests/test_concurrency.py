"""Two scans can overlap. These are the guards that stop that hurting.

The menu-bar app scans on launch and every six hours, so an automatic scan
being underway when someone starts one by hand is ordinary, not exotic. It
happened during development and crashed a scan outright.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.pipeline import _acquire_scan_lock, _release_scan_lock  # noqa: E402


def _receipt(message_id: str) -> dict:
    return {
        "watcher_id": "yesim",
        "gmail_message_id": message_id,
        "gmail_thread_id": "t1",
        "vendor": "Yesim",
        "reference": "5036549010148143",
        "purchased_on": "2026-08-14",
        "total": 8.64,
        "currency": "GBP",
        "filename": "receipt.pdf",
    }


def test_staging_the_same_email_twice_is_a_no_op() -> None:
    """The loser of the race must return 0, not raise IntegrityError."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "receipts.sqlite3")

        first = db.insert_receipt(_receipt("msg-1"))
        second = db.insert_receipt(_receipt("msg-1"))

        assert first > 0, "first insert should return a real row id"
        assert second == 0, f"duplicate should return 0, got {second}"
        assert len(db.list_receipts()) == 1, "only one row should exist"


def test_different_emails_still_insert() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "receipts.sqlite3")
        assert db.insert_receipt(_receipt("msg-1")) > 0
        assert db.insert_receipt(_receipt("msg-2")) > 0
        assert len(db.list_receipts()) == 2


def test_the_same_invoice_number_is_not_staged_twice() -> None:
    """`ingest` keys on the RFC Message-ID, `scan` on Gmail's id.

    The same email arriving by both routes carries different message ids but
    the same supplier invoice number, and would otherwise be filed twice.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "receipts.sqlite3")
        db.insert_receipt(_receipt("rfc-message-id-header"))

        assert db.has_reference("yesim", "5036549010148143")
        assert not db.has_reference("yesim", "some-other-number")
        assert not db.has_reference("trainline", "5036549010148143"), (
            "references should not collide across suppliers"
        )


def test_a_blank_reference_never_counts_as_a_duplicate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "receipts.sqlite3")
        assert not db.has_reference("yesim", "")


def test_only_one_scan_can_hold_the_lock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = SimpleNamespace(data_dir=Path(tmp))

        first = _acquire_scan_lock(config)
        assert first is not None, "first scan should get the lock"

        second = _acquire_scan_lock(config)
        assert second is None, "a concurrent scan must be turned away"

        _release_scan_lock(first)

        third = _acquire_scan_lock(config)
        assert third is not None, "lock must be reusable once released"
        _release_scan_lock(third)


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
