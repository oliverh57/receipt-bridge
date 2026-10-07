"""FreeAgent subscription invoice tests.

This is the first supplier that attaches its own invoice, which exercises two
capabilities nothing else uses: reading field values out of an attached PDF
(`source: attachment_text`) and filing that PDF as the receipt rather than a
render of the covering email.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.email_message import Email  # noqa: E402
from app.watchers import Watcher, build_filename  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
FREEAGENT_EML = FIXTURES / "freeagent-receipt.eml"
YESIM_EML = FIXTURES / "yesim-receipt.eml"


def load() -> tuple[Watcher, Email]:
    watcher = Watcher.from_file(ROOT / "watchers" / "freeagent.yaml")
    message = Email.from_eml(FREEAGENT_EML)
    return watcher, message


def test_matches_the_receipt_email() -> None:
    watcher, message = load()
    assert watcher.matches(message)


def test_the_invoice_pdf_is_attached() -> None:
    _, message = load()
    attachments = message.pdf_attachments("FreeAgent-Invoice.*\\.pdf")
    assert attachments, "the receipt email should carry the invoice PDF"
    assert attachments[0]["data"], "attachment should have bytes"


def test_reads_the_invoice_number_from_inside_the_pdf() -> None:
    """The invoice number appears nowhere in the email body."""
    watcher, message = load()
    values = watcher.extract(message)

    assert values["reference"] == "001756271"
    assert "001756271" not in message.text, (
        "if this ever appears in the body, the attachment_text source is "
        "no longer what is being exercised here"
    )


def test_extracts_the_vat_breakdown() -> None:
    """A real VAT invoice — 20% reclaimable, unlike zero-rated rail fares."""
    watcher, message = load()
    values = watcher.extract(message)

    assert values["total"] == 6.00
    assert values["net"] == 5.00
    assert values["vat"] == 1.00
    assert round(values["net"] + values["vat"], 2) == values["total"]


def test_description_names_the_billing_period() -> None:
    """The period is in the subject only. Searching the body found nothing,
    and the template printed the word None into every description."""
    watcher, message = load()
    assert watcher.extract(message)["description"] == "FreeAgent subscription, August 2026"


def test_a_missing_template_value_leaves_no_trace() -> None:
    rendered = Watcher._render_template("FreeAgent subscription, {period}", {"period": None}, "")
    assert rendered == "FreeAgent subscription"


def test_parses_the_two_digit_year() -> None:
    """'20 Aug 26' is 2026, not 1926."""
    watcher, message = load()
    assert watcher.extract(message)["purchased_on"] == "2026-08-20"


def test_builds_a_sensible_filename() -> None:
    watcher, message = load()
    filename = build_filename(watcher.filename, watcher.extract(message))
    assert filename == "2026-08-20 FreeAgent GBP6.00 001756271.pdf"
    assert not set(filename) & set('<>:"/\\|?*')


def test_ignores_a_failed_payment_notice() -> None:
    watcher, message = load()
    failed = Email(
        message_id="f",
        subject="FreeAgent - your receipt for August 2026",
        sender="FreeAgent <support@freeagent.com>",
        html=message.html.replace(
            "was successfully taken", "payment has failed and was not taken"
        ),
    )
    assert not watcher.matches(failed)


def test_does_not_claim_another_suppliers_email() -> None:
    watcher, _ = load()
    assert not watcher.matches(Email.from_eml(YESIM_EML))


def test_attachment_text_is_empty_not_fatal_without_attachments() -> None:
    """A missing or unreadable attachment must not break extraction."""
    plain = Email(
        message_id="p",
        subject="FreeAgent - your receipt for August 2026",
        sender="FreeAgent <support@freeagent.com>",
        html="<p>no attachment here</p>",
    )
    assert plain.attachment_text == ""


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
