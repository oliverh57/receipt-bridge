"""Building supplier rules from one example email.

The strongest test available: given only each fixture email, the builder
must reproduce what the hand-written rules for those suppliers produce.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import supplier_builder as sb  # noqa: E402
from app.email_message import Email  # noqa: E402
from app.watchers import Watcher, _parse_date  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _rebuild(fixture: str, **override) -> tuple[dict, Email]:
    message = Email.from_eml(FIXTURES / fixture)
    a = sb.analyse(message)
    amount = a.amounts[0]
    ref = a.references[0] if a.references else None
    spec = sb.build_spec({
        "name": a.name, "domain": a.domain, "subject": a.subject_hint,
        "amount_pattern": amount.pattern, "amount_source": amount.source,
        "currency": amount.currency,
        "reference_pattern": ref.pattern if ref else "",
        "reference_source": ref.source if ref else "text",
        "use_attachment": a.has_pdf, **override,
    })
    return spec, message


def test_rebuilds_freeagent_including_the_number_inside_the_pdf() -> None:
    spec, message = _rebuild("freeagent-receipt.eml")
    assert sb.preview(spec, message)["filename"] == "2026-08-20 FreeAgent GBP6.00 001756271.pdf"
    assert spec["pdf"][0] == {"step": "attachment"}


def test_rebuilds_yesim_from_a_table_layout() -> None:
    """Label and value sit on separate lines once the HTML table is flattened."""
    spec, message = _rebuild("yesim-receipt.eml", name="Yesim", mentions="Yesim")
    assert sb.preview(spec, message)["filename"] == "2026-08-14 Yesim GBP8.64 5036549010148143.pdf"


def test_rebuilds_trainline() -> None:
    spec, message = _rebuild("trainline-booking-confirmation.eml")
    assert sb.preview(spec, message)["filename"] == "2026-09-03 Trainline GBP19.95 009855653737.pdf"


def test_patterns_follow_the_label_not_the_literal_amount() -> None:
    """A rule that matched "£19.95" literally would only find one receipt."""
    spec, message = _rebuild("trainline-booking-confirmation.eml")
    changed = Email(
        message_id="x", subject=message.subject, sender=message.sender,
        html=message.html.replace("19.95", "42.10"),
    )
    assert Watcher.from_dict(spec).extract(changed)["total"] == 42.10


def test_mentions_keeps_other_merchants_out() -> None:
    """Payment processors send for many shops; the merchant is in the body."""
    spec, _ = _rebuild("yesim-receipt.eml", name="Yesim", mentions="Yesim")
    other = Email(
        message_id="o", subject="Your Receipt", sender="noreply@ecommpay.com",
        html="<p>Amount</p><p>42.00 GBP</p><p>Payment ID</p><p>1234567890123</p><p>SomeOtherShop</p>",
    )
    assert not Watcher.from_dict(spec).matches(other)
    assert '"Yesim"' in spec["gmail_query"], "the Gmail search should narrow too"


QUICKBOOKS_PDF = """ACTIVITY QTY RATE VAT AMOUNT
Personal Tax Return
1 75.00 20.0% S 75.00
SUBTOTAL 75.00
VAT TOTAL 15.00
TOTAL 90.00
BALANCE DUE GBP 90.00
VAT @ 20% 15.00 75.00
"""


def test_finds_totals_without_a_currency_symbol() -> None:
    """QuickBooks writes "GBP 90.00" and "TOTAL 90.00"; neither was seen."""
    message = Email(
        message_id="q", subject="Invoice 12345 from Example Accountants Ltd",
        sender="Example Accountants Ltd <quickbooks@notification.intuit.com>",
        plain="INVOICE NO. 90440\nDUE 01/10/2026\nGBP 90.00\nReview and pay",
        attachments=[{"filename": "Invoice_90440.pdf", "content_type": "application/pdf", "data": b"x"}],
        _attachment_text_cache=QUICKBOOKS_PDF,
    )
    amounts = sb.analyse(message).amounts
    assert (amounts[0].label, amounts[0].value, amounts[0].currency) == ("TOTAL", "90.00", "GBP")
    assert {"90.00"} == {c.value for c in amounts if c.source == "text"}
    assert "1" not in {c.value for c in amounts}, "unlabelled figures are not offered"


def _quickbooks(pdf: str = QUICKBOOKS_PDF + "VAT Registration No.: 162955976\n") -> Email:
    return Email(
        message_id="q", subject="Invoice 12345 from Example Accountants Ltd",
        sender="Example Accountants Ltd <quickbooks@notification.intuit.com>",
        plain="INVOICE NO. 90440\nGBP 90.00",
        attachments=[{"filename": "Invoice_90440.pdf", "content_type": "application/pdf", "data": b"x"}],
        _attachment_text_cache=pdf,
    )


def _quickbooks_choices(a: sb.Analysis, **override) -> dict:
    return {"name": "Example", "domain": a.domain, "amount_pattern": a.amounts[0].pattern,
            "amount_source": a.amounts[0].source, "currency": "GBP", **override}


def test_subtotal_and_vat_are_offered_after_the_total() -> None:
    """The general rules take the best-scoring total when a rule misses."""
    labels = [c.label for c in sb.analyse(_quickbooks()).amounts]
    assert labels.index("TOTAL") < labels.index("SUBTOTAL")
    assert labels.index("TOTAL") < labels.index("VAT TOTAL")
    assert sb._total_score("Total incl. VAT") == 100
    assert sb._total_score("Total excl. VAT") < 100


def test_vat_is_read_so_the_reclaim_isnt_lost() -> None:
    """Without a VAT field the invoice filed at 0%: £15 a month unclaimed."""
    message = _quickbooks()
    a = sb.analyse(message)
    assert [(c.label, c.value) for c in a.vat_amounts] == [("VAT TOTAL", "15.00")]
    vat = a.vat_amounts[0]
    spec = sb.build_spec(_quickbooks_choices(a, vat_pattern=vat.pattern, vat_source=vat.source))
    result = sb.preview(spec, message)
    assert (result["vat"], result["vat_rate"], result["vat_number"]) == (15.0, "20%", "GB162955976")


def test_a_figure_that_isnt_uk_vat_is_refused_before_saving() -> None:
    message = _quickbooks()
    a = sb.analyse(message)
    subtotal = next(c for c in a.amounts if c.label == "SUBTOTAL")
    spec = sb.build_spec(_quickbooks_choices(a, vat_pattern=subtotal.pattern, vat_source=subtotal.source))
    assert not sb.preview(spec, message)["ok"]


def test_a_month_without_its_vat_is_held_for_checking() -> None:
    """Filed at 0% instead, the reclaim would go without anyone noticing."""
    from app.pipeline import fill_from_text

    a = sb.analyse(_quickbooks())
    vat = a.vat_amounts[0]
    watcher = Watcher.from_dict(sb.build_spec(_quickbooks_choices(a, vat_pattern=vat.pattern, vat_source=vat.source)))
    values = watcher.extract(_quickbooks(QUICKBOOKS_PDF.replace("VAT TOTAL 15.00\n", "")), strict=False)
    assert any("VAT not found" in f for f in fill_from_text(values, _quickbooks()))


def test_paid_with_is_written_unless_its_the_default() -> None:
    a = sb.analyse(_quickbooks())
    assert "paid_with" not in sb.build_spec(_quickbooks_choices(a, paid_with="business"))
    assert sb.build_spec(_quickbooks_choices(a, paid_with="personal"))["paid_with"] == "personal"
    try:
        sb.build_spec(_quickbooks_choices(a, paid_with="Mettle"))
        raise AssertionError("an unknown account was accepted")
    except ValueError:
        pass


def test_a_total_label_never_matches_inside_another() -> None:
    """Picking "TOTAL" must not file the subtotal or the VAT line instead."""
    message = Email(message_id="q", plain=QUICKBOOKS_PDF.replace("90.00", "123.45"))
    for c in sb._amounts(message.text):
        spec = sb.build_spec({"name": "Q", "domain": "q.com", "amount_pattern": c.pattern})
        assert Watcher.from_dict(spec).extract(message)["total"] == float(c.value), c.label


def test_supplier_name_skips_generic_sender_words() -> None:
    assert sb._supplier_name("Uber Receipts", "uber.com") == "Uber"
    assert sb._supplier_name("", "mail.example.co.uk") == "Example"
    assert sb._registrable("email.amazon.co.uk") == "amazon.co.uk"
    assert sb._supplier_name("Anthropic, PBC", "mail.anthropic.com") == "Anthropic"
    assert sb._supplier_name("Acme Widgets Ltd", "acme.co.uk") == "Acme Widgets"


def test_subject_hint_drops_the_changing_parts() -> None:
    assert sb._subject_hint("Your receipt for order 123-456") == "Your receipt for order"
    assert sb._subject_hint("Your receipt from Anthropic, PBC #2571-0160-6198") == "Your receipt from Anthropic, PBC"
    # The month changes every invoice; a rule containing it breaks next month.
    assert sb._subject_hint("FreeAgent - your receipt for August 2026") == "FreeAgent - your receipt for"


def test_subject_hint_learns_from_the_senders_other_emails() -> None:
    """The product name in an Amazon subject changes with every order."""
    assert sb._subject_hint(
        "Ordered: ‘Clearhill 5V USB COB LED...’",
        ["Ordered: ‘SANDISK Ultra…’", "Dispatched: ‘NEW'C…’", "Ordered: ‘NEW'C 3 Pack…’"],
    ) == "Ordered"
    assert sb._subject_hint(
        "Your booking confirmation for return trip London to Whitstable",
        ["Your booking confirmation for single trip Tottenham Hale to Stansted"],
    ) == "Your booking confirmation for"


def test_other_kinds_of_email_from_the_sender_are_ignored() -> None:
    """A security notice must not shrink the hint to just the brand name."""
    assert sb._subject_hint(
        "FreeAgent - your receipt for August 2026",
        ["FreeAgent - your receipt for July 2026", "FreeAgent - New login to your account"],
    ) == "FreeAgent - your receipt for"


def test_invisible_characters_do_not_hide_references() -> None:
    """Amazon puts a right-to-left mark before every order number."""
    from app.email_message import html_to_text

    text = html_to_text("<p>Order # \u202b202-0912186-2580346</p><p>\u034f\u200c\u00ad</p>")
    assert "Order # 202-0912186-2580346" in text
    assert [c.value for c in sb._references(text)] == ["202-0912186-2580346"]


def test_saving_never_overwrites_an_existing_rule() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        spec, _ = _rebuild("trainline-booking-confirmation.eml")
        first = sb.save(dict(spec), folder)
        second = sb.save(dict(spec), folder)
        assert first != second and first.exists() and second.exists()
        assert Watcher.from_file(second).id == "trainline-2"


def test_iso_dates_are_never_read_day_first() -> None:
    """dayfirst parsing turned the email date 2026-09-03 into 9 March."""
    assert _parse_date("2026-09-03", []) == "2026-09-03"
    assert _parse_date("03/09/2026", []) == "2026-09-03"


def test_the_subject_suggested_is_a_piece_of_the_real_subject() -> None:
    from app.supplier_builder import _subject_hint

    hint = _subject_hint("Your Thursday evening trip with Uber",
                         ["Your Monday morning trip with Uber", "Your Friday evening trip with Uber"])
    assert hint and hint.lower() in "your thursday evening trip with uber", hint
    assert _subject_hint("Your Thursday evening trip with Uber") == "evening trip with Uber"
    assert _subject_hint("Your booking confirmation for 12 Oct") == "Your booking confirmation for"


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
