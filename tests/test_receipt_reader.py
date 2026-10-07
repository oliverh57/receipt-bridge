"""receipt-reader end to end: compile the Swift helper, OCR real photos,
read them. Needs macOS with Vision; no network. Slower than the other
tests (a few seconds per photo), so it reads a handful, not all 47.
"""

from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.receipt_reader import choose_copy, lost, read_file  # noqa: E402
from app.receipt_text import CONFIRMED, read_rows  # noqa: E402

PHOTOS = Path(__file__).parent / "fixtures" / "photos"
DATA = ROOT / "data"     # the helper is built here, as in the app


def test_reads_a_tilted_photo() -> None:
    """Post Office is photographed ~3° off; rows are rebuilt along the slope,
    so its VAT number sits on the same row as its label."""
    reading = read_file(PHOTOS / "post-office.webp", DATA)
    assert abs(reading.skew_degrees) > 1, reading.skew_degrees
    assert reading.text.total == Decimal("8.75")
    assert reading.text.vat_number == "GB172670502"


def test_reads_japanese() -> None:
    reading = read_file(PHOTOS / "yodobashi-akiba-split-payment.webp", DATA)
    assert reading.text.currency == "JPY"
    assert reading.text.total == Decimal("15300")
    assert reading.text.total_status == CONFIRMED


def test_reads_a_screenshot_e_receipt() -> None:
    reading = read_file(PHOTOS / "lime-ereceipt.png", DATA)
    assert reading.text.total == Decimal("3.99")
    assert reading.text.vat == Decimal("0.67")


def test_writes_a_jpeg_sized_for_freeagent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "receipt.jpg"
        reading = read_file(PHOTOS / "sandwi-bristol.webp", DATA, jpeg_out=out)
        assert reading.jpeg == out and out.exists()
        assert out.read_bytes()[:2] == b"\xff\xd8", "not a JPEG"
        assert out.stat().st_size < 5 * 1024 * 1024, "over FreeAgent's 5 MB limit"


def test_screenshots_carry_no_photo_date() -> None:
    """Fixtures came from the web, not a camera: no EXIF capture date, so
    the date cross-check (PLAN.md §5.4) correctly has nothing to compare."""
    assert read_file(PHOTOS / "greggs.webp", DATA).photo_taken is None



# ---- tidied copies --------------------------------------------------------------

def test_a_clear_receipt_photo_is_cropped_and_reads_the_same() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        reading = read_file(PHOTOS / "greggs.webp", DATA, jpeg_out=Path(tmp) / "g.jpg",
                            clean_prefix=Path(tmp) / "g")
        kinds = [c.kind for c in reading.clean]
        assert kinds == ["crop", "enhance"], (kinds, reading.clean_note)
        copy, notes = choose_copy(reading)
        assert copy is not None and copy.kind == "crop", notes
        assert copy.path.exists() and copy.path.read_bytes()[:2] == b"\xff\xd8"
        assert read_rows(copy.rows).total == reading.text.total


def test_no_crop_when_text_runs_to_the_edge() -> None:
    """Black Cat's slip: the edge Vision finds cuts through printed lines,
    so no crop is offered, only the contrast-only copy."""
    with tempfile.TemporaryDirectory() as tmp:
        reading = read_file(PHOTOS / "black-cat-card-slip.webp", DATA, jpeg_out=Path(tmp) / "b.jpg",
                            clean_prefix=Path(tmp) / "b")
        assert [c.kind for c in reading.clean] == ["enhance"]
        assert "edge" in (reading.clean_note or ""), reading.clean_note


def test_a_copy_that_misreads_a_digit_has_lost_something() -> None:
    original = ["ZU ZAHLEN   1,67 €", "KARTENZAHLUNG   1,67 €"]
    misread = ["ZU ZAHLEN   1,57 €", "KARTENZAHLUNG   1,67 €"]
    assert lost(read_rows(original), original, read_rows(misread), misread)


def test_a_point_read_as_a_gap_is_not_a_loss() -> None:
    """The digits are all there; OCR saw the point as a space."""
    original = ["Kwells   4.40", "TOTAL TO PAY   £11.40", "CARD SALES   £11.40"]
    copy = ["Kwells   4   40", "TOTAL TO PAY   £11.40", "CARD SALES   £11.40"]
    assert lost(read_rows(original), original, read_rows(copy), copy) == []


def test_a_copy_that_loses_the_total_or_vat_number_is_turned_down() -> None:
    original = ["TOTAL   £8.75", "VAT No. GB 172 6705 02"]
    copy = ["TOTAL   £8.75", "VAT No. GB 172 67"]
    assert "VAT number" in lost(read_rows(original), original, read_rows(copy), copy)
    assert "total" in lost(read_rows(original), original, read_rows(["TOTAL"]), ["TOTAL"])


def test_a_copy_reading_more_than_the_original_is_fine() -> None:
    original = ["TOTAL   £8.75"]
    copy = ["TOTAL   £8.75", "VAT No. GB172670502"]
    assert lost(read_rows(original), original, read_rows(copy), copy) == []

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
