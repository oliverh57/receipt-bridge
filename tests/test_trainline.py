"""Watcher tests that run against a real saved email, no network needed.

Drop any `.eml` into tests/fixtures/ and point a test at it — this is also the
fastest way to develop a new watcher: save the email, write the YAML, run this.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.email_message import Email  # noqa: E402
from app.watchers import Watcher, build_filename  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
TRAINLINE_EML = FIXTURES / "trainline-booking-confirmation.eml"


def load() -> tuple[Watcher, Email]:
    watcher = Watcher.from_file(ROOT / "watchers" / "trainline.yaml")
    message = Email.from_eml(TRAINLINE_EML)
    return watcher, message


def test_matches_the_confirmation_email() -> None:
    watcher, message = load()
    assert watcher.matches(message), "trainline watcher should match its own email"


def test_extracts_payment_fields() -> None:
    watcher, message = load()
    values = watcher.extract(message)

    assert values["reference"] == "009855653737"
    assert values["total"] == 19.95
    assert values["currency"] == "GBP"
    # Transaction date, not travel date — this is what the bank line shows.
    assert values["purchased_on"] == "2026-09-03"
    assert values["origin"] == "Tottenham Hale"
    assert values["destination"] == "Cambridge North"
    assert values["description"] == "Train travel, Tottenham Hale to Cambridge North"
    assert values["card_type"] == "MasterCard Debit"
    assert values["payment_type"] == "Apple Pay"


def test_finds_the_passwordless_order_link() -> None:
    watcher, message = load()
    values = watcher.extract(message)
    assert "my-account/order-token#" in values["order_link"]


def test_builds_a_sensible_filename() -> None:
    watcher, message = load()
    values = watcher.extract(message)
    filename = build_filename(watcher.filename, values)

    assert filename == (
        "2026-09-03 Trainline GBP19.95 Tottenham Hale to "
        "Cambridge North 009855653737.pdf"
    )
    assert not set(filename) & set('<>:"/\\|?*')


def test_rejects_an_unrelated_email() -> None:
    watcher, _ = load()
    other = Email(
        message_id="x",
        subject="Your Amazon order has shipped",
        sender="ship-confirm@amazon.co.uk",
        html="<p>Nothing to do with trains</p>",
    )
    assert not watcher.matches(other)


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
