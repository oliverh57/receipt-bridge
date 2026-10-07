"""Receipt-reading rules, run against 47 real receipt photos. No network,
no OCR: each photo's OCR text is saved in fixtures/photos/ocr/.

The hard gate is **zero wrong values**. A field left blank goes to Needs
attention for a person; a wrong value goes into the books. Accuracy floors
catch regressions in how much is read; any change that makes a field
*wrong* on any receipt fails outright.

The answers are hand-checked, in fixtures/photos/expected*.yaml. Sets 1–4
were held out and scored blind before the rules saw them (PLAN.md §5.2).
New receipts should be scored blind the same way before being added here.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.receipt_text import (  # noqa: E402
    CONFIRMED,
    MISSING,
    ReceiptText,
    find_dates,
    read_text,
    same_purchase,
)

PHOTOS = Path(__file__).parent / "fixtures" / "photos"
OCR = PHOTOS / "ocr"
FIELDS = ["total", "currency", "vat_number", "vat", "date"]

# Fields right across all 47 when this suite was written. Raise these as the
# rules improve; never lower them to make a change pass.
FLOORS = {"total": 46, "currency": 46, "vat_number": 46, "vat": 47, "date": 47}


def load_expected() -> dict[str, dict]:
    expected: dict[str, dict] = {}
    for path in sorted(PHOTOS.glob("expected*.yaml")):
        expected.update(yaml.safe_load(path.read_text()))
    return expected


def read_fixture(name: str) -> ReceiptText:
    return read_text((OCR / (Path(name).stem + ".txt")).read_text())


def judge(field: str, want, got: ReceiptText) -> str:
    """'right', 'blank' (safe: left for a person) or 'wrong' (dangerous)."""
    if field == "total":
        g = got.total
        if g is None:
            return "blank"
        return "right" if want is not None and Decimal(str(want)) == g else "wrong"
    if field == "currency":
        if got.currency is None:
            # unknown, asked in review: right only if a "$" correctly flagged dollars
            return "right" if got.currency_hint == "$" and want in ("USD", "AUD", "NZD", "CAD") else "blank"
        return "right" if got.currency == want else "wrong"
    if field == "vat_number":
        w = re.sub(r"\D", "", str(want)) if want else None
        g = re.sub(r"\D", "", got.vat_number) if got.vat_number else None
        if g is None:
            return "right" if w is None else "blank"
        return "right" if g == w else "wrong"
    if field == "vat":
        # "not printed" and £0 file the same way: no VAT reclaimed
        w = Decimal(str(want)) if want else Decimal(0)
        g = got.vat or Decimal(0)
        if g == w:
            return "right"
        return "blank" if g == 0 else "wrong"
    if field == "date":
        w = want if isinstance(want, date) else None
        if got.date is None:
            return "right" if w is None else "blank"
        return "right" if got.date == w else "wrong"
    raise ValueError(field)


def score() -> tuple[dict[str, int], list[str], list[str]]:
    right = {f: 0 for f in FIELDS}
    wrong: list[str] = []
    blanks: list[str] = []
    for name, want in load_expected().items():
        got = read_fixture(name)
        for f in FIELDS:
            verdict = judge(f, want.get(f), got)
            if verdict == "right":
                right[f] += 1
            elif verdict == "wrong":
                wrong.append(f"{name}: {f} read as {getattr(got, f)!r}, expected {want.get(f)!r}")
            else:
                blanks.append(f"{name}: {f}")
    return right, wrong, blanks


# ---- the gate ------------------------------------------------------------------

def test_every_fixture_has_ocr_text() -> None:
    missing = [n for n in load_expected() if not (OCR / (Path(n).stem + ".txt")).exists()]
    assert not missing, f"no OCR text for {missing}"


def test_no_wrong_values_on_any_receipt() -> None:
    _, wrong, _ = score()
    assert not wrong, "wrong values (these would go into the books):\n  " + "\n  ".join(wrong)


def test_accuracy_has_not_regressed() -> None:
    right, _, blanks = score()
    low = {f: (right[f], FLOORS[f]) for f in FIELDS if right[f] < FLOORS[f]}
    assert not low, f"fields now read on fewer receipts (got, floor): {low}; blanks: {blanks}"


# ---- guard rails, each from a real failure ------------------------------------------

def test_a_vat_number_is_never_read_as_a_vat_amount() -> None:
    """Greggs: "VAT: GB659880474" became £659,880,474 of VAT."""
    got = read_text("Total   3.40\nCard Payment   3.40\nVAT: GB659880474")
    assert got.vat is None
    assert got.vat_number == "GB659880474"


def test_vat_above_a_sixth_of_the_total_is_rejected() -> None:
    got = read_text("TOTAL   £6.00\nCARD   £6.00\nVAT   £2.00")
    assert got.vat is None, "£2.00 VAT on £6.00 is impossible at 20%"


def test_a_date_inside_a_longer_number_is_ignored() -> None:
    """Lime: "GB-2025-07-2044986992" gave 20 Jul 2025."""
    got = read_text("Total £3.99\nDocument Number: GB-2025-07-2044986992\nDate of issue: 29 Jul 2025")
    assert got.date == date(2025, 7, 29)


def test_the_receipt_decides_day_or_month_first() -> None:
    """Libertad: an unambiguous 29/04/2025 makes 04/05/2025 the 4th of May,
    even with "USD" printed on it."""
    got = read_text("04/05/2025 00:51\n29/04/2025 item\nARS 25,425.00\nUSD 21.37")
    assert got.date == date(2025, 5, 4)
    us = read_text("Ordered: 11/18/24 1:22 PM\nTotal $16.51\n12/01/24")
    assert date(2024, 12, 1) in [us.date, *us.other_dates], "11/18/24 proves month-first"


def test_a_torn_date_is_left_blank() -> None:
    assert read_text("Total £23.80\nJess   23 J 2025 18:57").date is None


def test_a_zero_read_for_the_o_of_a_month_is_still_a_date() -> None:
    """Sainsbury's prints "05OCT2026"; the camera read "050CT2026", so the
    photo had no date and couldn't be matched to its payment."""
    assert find_dates("#5281 13:12:11 050CT2026", "GBP") == [date(2026, 10, 5)]
    assert find_dates("1N0V2026", "GBP") == [date(2026, 11, 1)]
    assert find_dates("REF 1500T2026", "GBP") == []


def test_a_card_chip_id_is_not_money() -> None:
    got = read_text("DEBIT MASTERCARD (A0000000041010)\nTotal:\n19,34 EUR")
    assert got.total == Decimal("19.34")


def test_masked_card_digits_are_not_amounts() -> None:
    got = read_text("Total   $13.76\nVisa DEBIT   xxxxxxxx6335")
    assert got.total == Decimal("13.76")


def test_one_payment_printed_twice_is_one_payment() -> None:
    """Pret: "MasterCard £14.25" and "Payment £14.25" are one £14.25."""
    got = read_text("MasterCard   £14.25\nNet Total:   £2.58\nPayment   £14.25")
    assert got.total == Decimal("14.25")


def test_split_payments_confirm_a_total() -> None:
    """Yodobashi: ¥1,980 card + ¥13,320 cash = ¥15,300."""
    got = read_text("合計   15,300\nVISA（1 カイ   ）   1,980\n差引き現金支払い額   13,320\nお預かり額   13,320")
    assert got.total == Decimal("15300") and got.total_status == CONFIRMED


def test_cash_tendered_minus_change_confirms_a_total() -> None:
    got = read_text("Total   $21.65\nCash Tendered   $22.00\nChange   $0.35")
    assert got.total == Decimal("21.65") and got.total_status == CONFIRMED


def test_two_totals_that_cannot_be_reconciled_are_left_for_a_person() -> None:
    got = read_text("Total:   19,34 EUR\nTotaal   € 58,00")
    assert got.total is None and got.total_status == MISSING
    assert set(got.candidate_totals) == {Decimal("19.34"), Decimal("58.00")}


def test_not_a_vat_receipt_never_has_a_vat_number() -> None:
    got = read_text("Order Total\n£3.19\nVAT No: 123 4567 89\nThis is not a VAT receipt")
    assert got.vat_number is None and got.not_vat_receipt


def test_foreign_tax_numbers_are_not_uk_vat_numbers() -> None:
    got = read_text("ALDI\nZU ZAHLEN   1,67 €\nUSt. ID: DE 158 668 627")
    assert got.currency == "EUR" and got.vat_number is None


def test_dollars_alone_leave_the_currency_to_be_settled() -> None:
    got = read_text("Total   $7.11")
    assert got.currency is None and got.currency_hint == "$"


# ---- duplicates ------------------------------------------------------------------------

def test_same_purchase_photographed_twice_is_caught() -> None:
    a = read_fixture("sainsburys-whitstable-1.webp")
    b = read_fixture("sainsburys-whitstable-1-full.webp")
    assert same_purchase(a, b), "same £4.49 and auth code 228556"


def test_look_alike_receipts_are_not_duplicates() -> None:
    a = read_fixture("ms-st-pancras-1.webp")
    b = read_fixture("ms-st-pancras-2.webp")
    assert not same_purchase(a, b)


def test_only_the_two_known_duplicates_match_across_all_receipts() -> None:
    names = list(load_expected())
    reads = {n: read_fixture(n) for n in names}
    matches = {frozenset((a, b)) for i, a in enumerate(names) for b in names[i + 1:]
               if same_purchase(reads[a], reads[b])}
    assert matches == {frozenset(("sainsburys-whitstable-1.webp", "sainsburys-whitstable-1-full.webp"))}, matches


def test_reprints_are_flagged() -> None:
    assert read_fixture("costa-heathrow-reprint.webp").reprint
    assert read_fixture("golden-goose-duplicate.webp").reprint
    assert not read_fixture("tesco-express.webp").reprint


if __name__ == "__main__":
    right, wrong, blanks = score()
    n = len(load_expected())
    print("  " + ", ".join(f"{f} {right[f]}/{n}" for f in FIELDS)
          + f"; wrong values: {len(wrong)}; blanks: {len(blanks)}")
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
