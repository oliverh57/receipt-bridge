"""Yesim watcher tests, run against a real saved receipt. No network needed.

Yesim's receipts are sent by ecommpay, a payment processor with many other
merchants, under the thoroughly generic subject "Your Receipt". Most of the
risk in this watcher is therefore about matching too much, not too little,
which is what the isolation tests below are guarding.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.email_message import Email  # noqa: E402
from app.watchers import Watcher, build_filename  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
YESIM_EML = FIXTURES / "yesim-receipt.eml"
TRAINLINE_EML = FIXTURES / "trainline-booking-confirmation.eml"


def load() -> tuple[Watcher, Email]:
    watcher = Watcher.from_file(ROOT / "watchers" / "yesim.yaml")
    message = Email.from_eml(YESIM_EML)
    return watcher, message


def test_matches_the_receipt_email() -> None:
    watcher, message = load()
    assert watcher.matches(message), "yesim watcher should match its own email"


def test_extracts_payment_fields() -> None:
    watcher, message = load()
    values = watcher.extract(message)

    assert values["reference"] == "5036549010148143"
    assert values["total"] == 8.64  # `type: money` coerces to a float
    assert values["currency"] == "GBP"
    assert values["purchased_on"] == "2026-08-14"
    assert values["order_id"] == "52b1673495f774483d886df60ffda30bf380"
    assert values["rrn"] == "002143214946"
    assert values["auth_code"] == "834101"
    assert values["card"] == "559806******2685"


def test_reads_the_currency_rather_than_assuming_it() -> None:
    """Amount is "8.64 GBP" — value first. eSIMs can bill in EUR or USD."""
    watcher, message = load()
    euro = Email(
        message_id="y",
        subject="Your Receipt",
        sender="noreply@ecommpay.com",
        html=message.html.replace("8.64 GBP", "12.40 EUR"),
    )
    values = watcher.extract(euro)
    assert values["total"] == 12.40
    assert values["currency"] == "EUR"


def test_handles_a_single_decimal_place() -> None:
    """ecommpay send "15.3 GBP", not "15.30".

    A strict two-decimal pattern with a bare-integer fallback matched "15"
    here and filed the wrong amount — worse than failing, because it
    reconciles silently. Regression test for a real receipt in the inbox.
    """
    watcher, message = load()
    short = Email(
        message_id="s",
        subject="Your Receipt",
        sender="noreply@ecommpay.com",
        html=message.html.replace("8.64 GBP", "15.3 GBP"),
    )
    values = watcher.extract(short)
    assert values["total"] == 15.3, f"expected 15.3, got {values['total']}"
    assert values["currency"] == "GBP"


def test_handles_a_whole_number_amount() -> None:
    watcher, message = load()
    whole = Email(
        message_id="w",
        subject="Your Receipt",
        sender="noreply@ecommpay.com",
        html=message.html.replace("8.64 GBP", "20 GBP"),
    )
    assert watcher.extract(whole)["total"] == 20


def test_parses_the_day_first_date() -> None:
    """14.08.2026 is 14 August, not 8 December."""
    watcher, message = load()
    assert watcher.extract(message)["purchased_on"] == "2026-08-14"


def test_builds_a_sensible_filename() -> None:
    watcher, message = load()
    values = watcher.extract(message)
    filename = build_filename(watcher.filename, values)

    assert filename == "2026-08-14 Yesim GBP8.64 5036549010148143.pdf"
    assert not set(filename) & set('<>:"/\\|?*')


def test_ignores_another_merchants_ecommpay_receipt() -> None:
    """The point of body_contains: same sender, same subject, not Yesim."""
    watcher, _ = load()
    other_merchant = Email(
        message_id="z",
        subject="Your Receipt",
        sender="noreply@ecommpay.com",
        html=(
            "<p>The payment is successfully completed</p>"
            "<p>Amount 42.00 GBP</p><p>Payment ID 1234567890123456</p>"
            "<p>Best Regards, SomeOtherShop Customer Support</p>"
        ),
    )
    assert not watcher.matches(other_merchant), (
        "a non-Yesim ecommpay receipt must not be collected"
    )


def test_ignores_a_refund_notice() -> None:
    watcher, message = load()
    refund = Email(
        message_id="r",
        subject="Your Receipt",
        sender="noreply@ecommpay.com",
        html=message.html.replace(
            "The payment is successfully completed",
            "The refund is successfully completed",
        ),
    )
    assert not watcher.matches(refund)


def test_does_not_claim_a_trainline_email() -> None:
    watcher, _ = load()
    assert not watcher.matches(Email.from_eml(TRAINLINE_EML))


def test_trainline_does_not_claim_a_yesim_email() -> None:
    trainline = Watcher.from_file(ROOT / "watchers" / "trainline.yaml")
    _, message = load()
    assert not trainline.matches(message)


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
