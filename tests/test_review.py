"""Corrections made in review: validated, settle the matching warnings, and
are remembered (category per supplier, supplier name per VAT number).
Made-up values; no network.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.filer as filer  # noqa: E402
from app.config import Config  # noqa: E402
from app.service import ReceiptService  # noqa: E402

CATEGORY = "https://fa.test/v2/categories/269"
FLAGS = ["Total couldn't be read. Type it in from the photo.",
         "No currency shown. Which currency?",
         "No date found. Enter it from the receipt.",
         "Supplier name guessed by the Mac. Check it.",
         "Paid from the business account, or personally?",
         "Looks like the same purchase as Example #3 (same amount and card authorisation code 123456)."]


def make() -> tuple[ReceiptService, int, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    service = ReceiptService(Config(raw={
        "data_dir": str(base / "data"), "export_dir": str(base / "export"),
        "photo_inbox": str(base / "inbox"), "freeagent": {"credentials_file": str(base / "none.json")}}))
    service.db.set_state("freeagent:reference", json.dumps(
        {"categories": [{"url": CATEGORY, "description": "Computer Software", "nominal_code": "269",
                         "group": "admin_expenses_categories"}]}))
    rid = service.db.insert_receipt({
        "watcher_id": "photo", "source": "photo", "source_id": "sha256:abc", "vendor": "EXAMPLE CAFE LTD",
        "vat_number": "GB123456789", "extra_json": {"flags": list(FLAGS)}})
    return service, rid, tmp


def flags(service: ReceiptService, rid: int) -> list[str]:
    return json.loads(service.db.get_receipt(rid)["extra_json"])["flags"]


def test_correcting_a_field_clears_only_its_warnings() -> None:
    service, rid, tmp = make()
    with tmp:
        service.set_receipt_fields(rid, {"total": "12.5", "currency": "eur"})
        row = service.db.get_receipt(rid)
        assert row["total"] == 12.5 and row["currency"] == "EUR"
        left = flags(service, rid)
        assert not any("Total" in f or "currency" in f for f in left), left
        assert any("same purchase" in f for f in left), "a duplicate warning isn't settled by a total"
        service.set_receipt_fields(rid, {"purchased_on": "2026-03-04", "vendor": "Example Cafe",
                                         "paid_by": "business"})
        assert flags(service, rid) == [FLAGS[-1]]


def test_a_guessed_supplier_you_checked_is_no_longer_a_guess() -> None:
    service, rid, tmp = make()
    with tmp:
        extra = json.loads(service.db.get_receipt(rid)["extra_json"])
        service.db.update_receipt(rid, {"extra_json": {**extra, "supplier_source": "model"}})
        same = service.db.get_receipt(rid)["vendor"]
        service.set_receipt_fields(rid, {"vendor": same})            # left as it was, but checked
        assert json.loads(service.db.get_receipt(rid)["extra_json"])["supplier_source"] == "you"
        assert not any("Supplier name" in f for f in flags(service, rid))


def test_suppliers_you_have_had_are_suggested_most_used_first() -> None:
    service, rid, tmp = make()
    with tmp:
        for i, name in enumerate(["Greggs", "greggs ", "Pret", "Greggs", ""]):
            service.db.insert_receipt({"watcher_id": "photo", "source": "photo", "source_id": f"sha256:n{i}",
                                       "vendor": name, "total": 1.0, "currency": "GBP"})
        for i, name in enumerate(["Boots", "Boots Ltd", "Boot", "Boots", "BOOTS UK Limited"]):
            service.db.insert_receipt({"watcher_id": "photo", "source": "photo", "source_id": f"sha256:b{i}",
                                       "vendor": name, "total": 1.0, "currency": "GBP"})
        names = service.supplier_names()
        assert names[0] == "Boots", names                 # five of them, most often spelt "Boots"
        assert names[1] == "Greggs" and "Pret" in names and "" not in names
        assert not any(n.lower().startswith("boot") for n in names[1:]), "one suggestion per supplier"
        assert sum(n.lower().strip() == "greggs" for n in names) == 1


def test_looks_right_settles_doubts_but_not_blanks() -> None:
    """Files' "Looks right": guessed supplier, possible duplicate… settled;
    a total, currency or payer that's still missing is not."""
    service, rid, tmp = make()
    with tmp:
        service.set_receipt_fields(rid, {"checked": True})
        left = flags(service, rid)
        assert any("Total" in f for f in left) and any("currency" in f for f in left)
        assert any("personally?" in f for f in left)
        assert not any("Supplier name" in f or "same purchase" in f for f in left), left
        service.set_receipt_fields(rid, {"total": 4.5, "currency": "GBP", "paid_by": "business"})
        service.set_receipt_fields(rid, {"checked": True})
        assert flags(service, rid) == [] or all("date" in f.lower() for f in flags(service, rid))


def test_an_expense_is_checked_in_files_like_any_file() -> None:
    """Paid personally doesn't skip the checks: missing details keep it in
    "To check"; once complete it's an expense to claim, still in Files."""
    service, rid, tmp = make()
    with tmp:
        service.set_receipt_fields(rid, {"paid_by": "personal"})
        item = next(r for r in service.receipts("pending") if r["id"] == rid)
        assert item["stage"] == "check" and "No total" in item["issues"], item["issues"]
        service.set_receipt_fields(rid, {"total": 4.5, "currency": "GBP", "purchased_on": "2026-03-04",
                                         "vendor": "Example Cafe"})
        service.set_receipt_fields(rid, {"checked": True})
        item = next(r for r in service.receipts("pending") if r["id"] == rid)
        assert item["stage"] == "expense", item["issues"]
        assert service.review_counts()["files"] == 1 and "expenses" not in service.review_counts()


def test_requests_kept_by_the_old_dry_run_are_forgotten() -> None:
    """Dry run is gone: what it prepared reads as not filed yet."""
    service, rid, tmp = make()
    with tmp:
        filed = service.db.insert_receipt({"watcher_id": "photo", "source": "photo", "source_id": "sha256:def"})
        service.db.update_receipt(rid, {"freeagent_json": json.dumps({"state": "dry_run", "body": {}})})
        service.db.update_receipt(filed, {"freeagent_json": json.dumps(
            {"state": "filed", "url": "e/1", "update": {"state": "dry_run", "parts": []}})})
        service.db.set_state("explained:t/1", json.dumps({"state": "dry_run", "body": {}}))
        service.db.set_state("explained:t/2", json.dumps({"state": "filed", "url": "e/2"}))
        service.db.set_state("freeagent:dry_run", "0")
        service.db.delete_state("tidied:dry_run")
        again = ReceiptService(service.config)
        assert again.db.get_receipt(rid)["freeagent_json"] is None
        assert json.loads(again.db.get_receipt(filed)["freeagent_json"]) == {"state": "filed", "url": "e/1"}
        assert again.db.get_state("explained:t/1") is None and again.db.get_state("explained:t/2")
        assert again.db.get_state("freeagent:dry_run") is None


def test_older_installs_read_two_years_of_payments_once() -> None:
    """They read only 120 days back: the next sync reads from the start."""
    service, _rid, tmp = make()
    with tmp:
        service.db.set_state("freeagent:last_sync", "2026-10-01T00:00:00+00:00")
        service.db.delete_state("tidied:history_two_years")
        again = ReceiptService(service.config)
        assert again.db.get_state("freeagent:last_sync") is None
        again.db.set_state("freeagent:last_sync", "2026-10-02T00:00:00+00:00")
        assert ReceiptService(service.config).db.get_state("freeagent:last_sync")


def test_switching_freeagent_company_forgets_the_old_ones_links() -> None:
    """Sandbox → your real books: nothing that points into the sandbox
    (categories, chosen payments, re-billing) is kept."""
    service, rid, tmp = make()
    with tmp:
        service.db.update_receipt(rid, {"category": CATEGORY, "transaction_url": "https://fa.test/v2/bank_transactions/1",
                                        "freeagent_json": "{}", "extra_json": {"rebill": {"project": "p"}, "flags": []}})
        service.db.set_state("category_for:example cafe", CATEGORY)
        service._check_company("sandbox:https://fa.test/v2/company")       # the first company: nothing to forget
        service._check_company("sandbox:https://fa.test/v2/company")
        assert service.db.get_receipt(rid)["category"] == CATEGORY
        service._check_company("production:https://api.freeagent.com/v2/company")
        row = service.db.get_receipt(rid)
        assert row["category"] is None and row["transaction_url"] is None and row["freeagent_json"] is None
        assert "rebill" not in json.loads(row["extra_json"])
        assert service.db.get_state("category_for:example cafe") is None
        assert row["vendor"] == "EXAMPLE CAFE LTD", "the receipt itself is kept"


def test_no_receipt_needed_explains_the_payment_with_its_own_settings() -> None:
    """A payment's category, VAT and re-billing are set on its Statement
    line; "No receipt needed" builds the explanation from them."""
    service, rid, tmp = make()
    with tmp:
        acct, url = "https://fa.test/v2/bank_accounts/1", "https://fa.test/v2/bank_transactions/9"
        ref = json.loads(service.db.get_state("freeagent:reference"))
        ref.update({"bank_accounts": [{"url": acct, "name": "Example Bank", "currency": "GBP"}],
                    "vat": {"registered": True}, "projects": [{"url": "https://fa.test/v2/projects/1", "name": "P"}]})
        service.db.set_state("freeagent:reference", json.dumps(ref))
        service.db.set_state("freeagent:accounts", json.dumps([acct]))
        service.db.save_bank_transactions([{"url": url, "bank_account": acct, "dated_on": "2026-03-04",
                                            "amount": "-3.30", "unexplained_amount": "-3.30", "description": "EXAMPLE TRAVEL"}])
        try:
            service.set_payment_settings(url, {"vat_rate": "17.5"})
            raise AssertionError("a made-up VAT rate was accepted")
        except ValueError:
            pass
        service.set_payment_settings(url, {"category": CATEGORY, "vat_rate": "0.0",
                                           "rebill": {"project": "https://fa.test/v2/projects/1", "type": "cost"}})
        sent = []
        service._freeagent = lambda: SimpleNamespace(connected=True)
        service._run_freeagent_sync = lambda: None
        original = filer.explain_without_receipt
        filer.explain_without_receipt = lambda c, u, body: sent.append(body) or "e/new"
        try:
            service._run_explain_payment(url, None)
        finally:
            filer.explain_without_receipt = original
        assert json.loads(service.db.get_state(f"explained:{url}"))["state"] == "filed"
        assert sent == [{"bank_transaction": url, "dated_on": "2026-03-04", "gross_value": "-3.30",
                                 "category": CATEGORY, "description": "EXAMPLE TRAVEL (no receipt)",
                                 "sales_tax_rate": "0.0", "project": "https://fa.test/v2/projects/1",
                                 "rebill_type": "cost"}]
        row = next(r for r in service.statement(acct, "2026-03")["rows"] if r["url"] == url)
        assert row["settings"]["category"] == CATEGORY and row["explained_here"]["state"] == "filed"


def test_a_payment_freeagent_explained_starts_from_its_explanation() -> None:
    """The Statement shows FreeAgent's own category, VAT and re-billing; only
    what you change is sent, and "Keep FreeAgent's" forgets it."""
    service, rid, tmp = make()
    with tmp:
        acct, url = "https://fa.test/v2/bank_accounts/1", "https://fa.test/v2/bank_transactions/7"
        project = "https://fa.test/v2/projects/1"
        ref = json.loads(service.db.get_state("freeagent:reference"))
        ref.update({"bank_accounts": [{"url": acct, "name": "Example Bank", "currency": "GBP"}],
                    "vat": {"registered": True}, "projects": [{"url": project, "name": "P"}]})
        service.db.set_state("freeagent:reference", json.dumps(ref))
        service.db.set_state("freeagent:accounts", json.dumps([acct]))
        service.db.save_bank_transactions([{"url": url, "bank_account": acct, "dated_on": "2026-03-03",
            "amount": "-3.30", "unexplained_amount": "0", "description": "EXAMPLE TRANSPORT",
            "bank_transaction_explanations": [{"url": "https://fa.test/e/1", "category": "c/travel",
                                               "sales_tax_rate": "20.0", "project": project,
                                               "rebill_type": "markup", "rebill_factor": "0.25"}]}])
        row = lambda: next(r for r in service.statement(acct, "2026-03")["rows"] if r["url"] == url)
        fa = row()["freeagent"]
        assert fa["explained"] and fa["category"] == "c/travel" and fa["vat_rate"] == "20.0"
        assert fa["rebill"] == {"project": project, "type": "markup", "factor": 25.0} and fa["changes"] == {}
        service.set_payment_settings(url, {"category": CATEGORY, "vat_rate": "0.0"})
        assert row()["freeagent"]["changes"] == {"category": CATEGORY, "sales_tax_rate": "0.0"}
        service.set_payment_settings(url, {"rebill": None})                  # stop re-billing
        assert row()["freeagent"]["changes"]["project"] is None
        sent = []
        service._freeagent = lambda: SimpleNamespace(connected=True)
        service._run_freeagent_sync = lambda: None
        original = filer.update_existing_explanation
        filer.update_existing_explanation = lambda c, u, exp, changes: sent.append(changes) or {}
        try:
            service._run_update_payment_explanation(url)
        finally:
            filer.update_existing_explanation = original
        assert sent[0]["category"] == CATEGORY and sent[0]["project"] is None
        service.set_payment_settings(url, {"category": CATEGORY})
        service.reset_payment_settings(url)
        assert row()["freeagent"]["changes"] == {}


def test_correcting_a_linked_payments_category_sticks() -> None:
    """Linked, then the category was wrong: changed in Bank Feed, sent to
    FreeAgent's explanation, kept on the receipt and for the supplier."""
    service, rid, tmp = make()
    with tmp:
        acct, url = "https://fa.test/v2/bank_accounts/1", "https://fa.test/v2/bank_transactions/5"
        other = "https://fa.test/v2/categories/285"
        ref = json.loads(service.db.get_state("freeagent:reference"))
        ref["bank_accounts"] = [{"url": acct, "name": "Example Bank", "currency": "GBP"}]
        ref["categories"].append({"url": other, "description": "Internet & Telephone", "nominal_code": "285",
                                  "group": "admin_expenses_categories"})
        service.db.set_state("freeagent:reference", json.dumps(ref))
        service.db.set_state("freeagent:accounts", json.dumps([acct]))
        service.db.save_bank_transactions([{"url": url, "bank_account": acct, "dated_on": "2026-09-16", "amount": "-16.94",
            "unexplained_amount": "0", "description": "Adobe",
            "bank_transaction_explanations": [{"url": "e/9", "category": CATEGORY, "sales_tax_rate": "20.0",
                                               "marked_for_review": False, "attachment": {"url": "a/1"}}]}])
        service.db.update_receipt(rid, {"category": CATEGORY, "freeagent_json": json.dumps(
            {"state": "filed", "transaction": url, "url": "e/9"})})
        service.db.set_status(rid, "filed")
        row = lambda: next(r for r in service.statement(acct, "2026-09")["rows"] if r["url"] == url)
        assert row()["status"] == "filed" and row()["freeagent"]["explained"]
        service.set_payment_settings(url, {"category": other})
        assert row()["freeagent"]["changes"] == {"category": other}
        sent = []
        service._freeagent = lambda: SimpleNamespace(connected=True)
        service._run_freeagent_sync = lambda: None
        original = filer.update_existing_explanation
        filer.update_existing_explanation = lambda c, u, exp, changes: sent.append(changes) or {}
        try:
            service._run_approve(url, None)
        finally:
            filer.update_existing_explanation = original
        assert sent == [{"category": other}]
        assert service.db.get_receipt(rid)["category"] == other
        assert service.db.get_state("category_for:example cafe ltd") == other


def test_an_email_for_a_payment_must_be_for_one_that_can_take_it() -> None:
    service, _rid, tmp = make()
    with tmp:
        try:
            service.add_email("", "m1", {"supplier": "Adobe", "total": "16.94", "payment_url": "tx/gone"})
            raise AssertionError("added for a payment that isn't there")
        except ValueError as exc:
            assert "isn't in the statement" in str(exc)


def test_the_receipt_can_be_any_pdf_or_picture_the_email_carries() -> None:
    """An insurer attaches its policy as a PDF: the receipt is what you pick,
    the policy, a photo, or the email itself. Never an SVG (it can hold script)."""
    from app.service import _email_fields, _receipt_attachment

    pdf = {"filename": "policy.pdf", "content_type": "application/pdf", "data": b"%PDF-1.4 policy"}
    photo = {"filename": "image0.jpeg", "content_type": "image/jpeg", "data": b"\xff\xd8 jpeg"}
    svg = {"filename": "logo.svg", "content_type": "image/svg+xml", "data": b"<svg/>"}
    message = SimpleNamespace(message_id="m1", attachments=[pdf, photo, svg],
                              pdf_attachments=lambda: [pdf])
    assert _receipt_attachment(message, "att:0")[1] == ".pdf" and _receipt_attachment(message, "att:1")[1] == ".jpeg"
    assert _receipt_attachment(message, "att:2") is None and _receipt_attachment(message, "att:9") is None
    service, _rid, tmp = make()
    with tmp:
        path, source = service._email_document(message, {"supplier": "X", "amount": 1, "day": ""}, "att:1")
        assert path.suffix == ".jpeg" and path.read_bytes() == photo["data"] and source == "attachment"
        path, _ = service._email_document(message, {"supplier": "X", "amount": 1, "day": ""}, "")
        assert path.read_bytes() == pdf["data"], "no choice: its PDF, as before"
    assert _email_fields({"document": "att:1"})["document"] == "att:1"
    try:
        _email_fields({"document": "../etc"})
        raise AssertionError("a made-up choice was accepted")
    except ValueError:
        pass


def test_an_email_added_for_a_payment_is_approved_straight_away() -> None:
    """Bank Feed's "Use that email" → Add and approve: linked in one go,
    with the category set on the payment."""
    service, _rid, tmp = make()
    with tmp:
        url = "tx/u"
        service.db.set_state(f"email_hint:{url}", json.dumps({"message_id": "m1"}))
        service.db.set_state(f"payment:{url}", json.dumps({"category": CATEGORY}))
        pdf = Path(tmp.name) / "r.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        service._mail_client = lambda account="": (SimpleNamespace(email="me@example.test"), None)
        service._cached_email = lambda chosen, client, mid: SimpleNamespace(thread_id="t", subject="Receipt",
                                                                            date_iso="2026-09-15")
        service._email_document = lambda message, hint, document="": (pdf, "rendered_email")
        approved = []
        service._run_approve = lambda u, rid: approved.append((u, rid))
        service._run_add_email("me@example.test", "m1", {"supplier": "Adobe", "date": "2026-09-15", "total": 16.94,
            "currency": "GBP", "vat": None, "vat_choice": "auto", "paid_by": "business", "payment_url": url})
        rid = approved[0][1]
        row = service.db.get_receipt(rid)
        assert approved == [(url, rid)] and row["transaction_url"] == url and row["category"] == CATEGORY
        assert service.db.get_state(f"email_hint:{url}") is None
        # chosen in the dialog: its category and project win
        other, project = "https://fa.test/v2/categories/285", "https://fa.test/v2/projects/1"
        service._run_add_email("me@example.test", "m2", {"supplier": "JustPark", "date": "2026-08-24", "total": 14.99,
            "currency": "GBP", "vat": None, "vat_choice": "auto", "paid_by": "business", "payment_url": "tx/j",
            "category": other, "project": project})
        row = service.db.get_receipt(approved[1][1])
        assert row["category"] == other and json.loads(row["extra_json"])["rebill"] == {"project": project, "type": "none",
                                                                                       "factor": None}


def test_an_ignored_email_is_remembered_and_not_suggested_for_a_payment() -> None:
    """Emails → right-click → Ignore: kept, undone by Don't ignore, and a
    payment's "An email may be the receipt" doesn't offer it."""
    service, _rid, tmp = make()
    with tmp:
        service.db.set_state("email_hint:tx/1", json.dumps({"message_id": "m1", "supplier": "Hotel", "amount": 9.0,
                                                             "day": "2026-09-01"}))
        assert service.email_suggestion("tx/1")["message_id"] == "m1"
        service.ignore_emails(["m1"])
        assert service.ignored_emails() == {"m1"} and service.email_suggestion("tx/1") is None
        service.ignore_emails(["m1"], ignored=False)
        assert service.ignored_emails() == set() and service.email_suggestion("tx/1") is not None


def test_a_payment_approved_in_freeagent_is_done() -> None:
    """Approved there (not a guess awaiting review): done in the Statement,
    with or without a receipt. A guess waiting for review still needs one."""
    service, rid, tmp = make()
    with tmp:
        acct = "https://fa.test/v2/bank_accounts/1"
        ref = json.loads(service.db.get_state("freeagent:reference"))
        ref["bank_accounts"] = [{"url": acct, "name": "Example Bank", "currency": "GBP"}]
        service.db.set_state("freeagent:reference", json.dumps(ref))
        service.db.set_state("freeagent:accounts", json.dumps([acct]))
        def tx(n, review):
            return {"url": f"t/{n}", "bank_account": acct, "dated_on": f"2026-03-0{n}", "amount": f"-{n}.00",
                    "unexplained_amount": "0", "description": "EXAMPLE",
                    "bank_transaction_explanations": [{"url": f"e/{n}", "category": "c", "marked_for_review": review}]}
        service.db.save_bank_transactions([tx(1, False), tx(2, True)])
        rows = {r["url"]: r for r in service.statement(acct, "2026-03")["rows"]}
        assert rows["t/1"]["status"] == "approved" and rows["t/1"]["approved"]
        assert rows["t/2"]["status"] == "missing" and not rows["t/2"]["approved"]
        summary = service.statement(acct, "2026-03")["summary"]
        assert summary["missing"] == 1 and summary["payments"] == 2


def test_rebill_to_a_client_is_validated_and_kept_per_receipt() -> None:
    service, rid, tmp = make()
    with tmp:
        ref = json.loads(service.db.get_state("freeagent:reference"))
        ref["projects"] = [{"url": "https://fa.test/v2/projects/7", "name": "Website", "client": "Example Client Ltd"}]
        service.db.set_state("freeagent:reference", json.dumps(ref))
        for bad in ({"project": "https://elsewhere/p/1"}, {"project": "https://fa.test/v2/projects/7", "type": "free"}, "yes"):
            try:
                service.set_receipt_fields(rid, {"rebill": bad})
            except ValueError:
                pass
            else:
                raise AssertionError(f"accepted {bad!r}")
        service.set_receipt_fields(rid, {"rebill": {"project": "https://fa.test/v2/projects/7", "type": "markup", "factor": "12.5"}})
        assert service._rebill(service.db.get_receipt(rid)) == {"project": "https://fa.test/v2/projects/7", "type": "markup", "factor": 12.5}
        assert flags(service, rid), "re-billing doesn't settle the receipt's doubts"
        service.set_receipt_fields(rid, {"rebill": None})
        assert service._rebill(service.db.get_receipt(rid)) is None


def test_bad_corrections_are_refused() -> None:
    service, rid, tmp = make()
    with tmp:
        for bad in ({"total": "-3"}, {"currency": "pounds"}, {"purchased_on": "4/3/2026"},
                    {"paid_by": "someone"}, {"category": "https://elsewhere/1"}, {"status": "filed"}):
            try:
                service.set_receipt_fields(rid, bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted {bad}")
        assert len(flags(service, rid)) == len(FLAGS)


def test_names_and_categories_are_remembered() -> None:
    service, rid, tmp = make()
    with tmp:
        service.set_receipt_fields(rid, {"vendor": "Example Cafe", "category": CATEGORY})
        assert service.db.get_state("supplier_by_vat:GB123456789") == "Example Cafe"
        assert service.db.get_state("category_for:example cafe") == CATEGORY
        other = service.db.insert_receipt({"watcher_id": "photo", "source": "photo", "source_id": "sha256:def",
                                           "vendor": "Example Cafe"})
        assert service._category_for(service.db.get_receipt(other)) == CATEGORY


def test_vat_treatment_is_a_supplier_setting() -> None:
    service, rid, tmp = make()
    with tmp:
        service.set_receipt_fields(rid, {"vat_treatment": "reverse_charge"})
        assert service._vat_treatment(service.db.get_receipt(rid)) == "reverse_charge"
        try:
            service.set_receipt_fields(rid, {"vat_treatment": "guess"})
        except ValueError:
            pass
        else:
            raise AssertionError("accepted an unknown VAT treatment")


def connected(service: ReceiptService, base: Path) -> None:
    """A made-up FreeAgent connection: credentials, a token, one account, two payments."""
    import time
    from app.freeagent import TokenStore
    creds = base / "fa.json"
    creds.write_text(json.dumps({"client_id": "x", "client_secret": "y", "environment": "sandbox"}))
    service.config.raw["freeagent"] = {"credentials_file": str(creds)}
    TokenStore(service.config.freeagent_token_file).save(
        {"environment": "sandbox", "access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3600})
    account = "https://fa.test/v2/bank_accounts/1"
    ref = json.loads(service.db.get_state("freeagent:reference"))
    ref.update({"vat": {"registered": True}, "bank_accounts": [{"url": account, "currency": "GBP", "name": "Business"}]})
    service.db.set_state("freeagent:reference", json.dumps(ref))
    service.db.set_state("freeagent:accounts", json.dumps([account]))
    service.db.save_bank_transactions([
        {"url": "tx/1", "bank_account": account, "dated_on": "2026-03-05", "amount": -21.5,
         "unexplained_amount": -21.5, "description": "RAILCO"},
        {"url": "tx/2", "bank_account": account, "dated_on": "2026-03-06", "amount": -9.0,
         "unexplained_amount": -9.0, "description": "CAFE"},
        {"url": "tx/3", "bank_account": account, "dated_on": "2026-03-07", "amount": -9.0,
         "unexplained_amount": -9.0, "description": "CAFE"},
    ])


def email_receipt(service: ReceiptService, n: int, vendor: str, day: str, total: float) -> int:
    return service.db.insert_receipt({"watcher_id": vendor.lower(), "gmail_message_id": f"m{n}", "vendor": vendor,
                                      "purchased_on": day, "total": total, "currency": "GBP",
                                      "pdf_path": __file__, "pdf_source": "attachment"})


def by_id(service: ReceiptService) -> dict[int, dict]:
    return {r["id"]: r for r in service.receipts("pending")}


def test_only_suppliers_you_approved_link_themselves() -> None:
    """No auto-linking unless you ticked "Link {supplier} automatically from
    now on"; filing one by hand never turns it on. Then: an exact named
    match, nothing to check."""
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        rail = email_receipt(service, 1, "Railco", "2026-03-04", 21.5)
        cafe = email_receipt(service, 2, "Cafe", "2026-03-05", 9.0)
        for rid in (rail, cafe):
            service.set_receipt_fields(rid, {"category": CATEGORY})
        item = by_id(service)[rail]
        assert item["group"] == "ready" and item["stage"] == "link" and item["auto_file"] is False
        assert service.auto_link_candidates() == [], "nobody approved these suppliers"
        service.set_receipt_fields(rail, {"auto_file": True})
        service.set_receipt_fields(cafe, {"auto_file": True})
        # Cafe: two identical £9 payments, nearest taken, never asked (§11)
        assert sorted(service.auto_link_candidates()) == sorted([rail, cafe])
        service.set_receipt_fields(cafe, {"auto_file": False})
        assert service.auto_link_candidates() == [rail]
        assert item["payment"]["chips"] == ["Amount", "Name", "Date"]
        assert item["will_file"] == {"type": "Bank explanation", "category": "Computer Software",
                                     "vat": "£0 (none shown)", "attachment": "Supplier PDF"}


def test_a_payment_you_chose_far_from_the_receipt_date_is_marked() -> None:
    """Choosing a payment 3 weeks away is allowed, but its date chip must
    not look like a same-day match."""
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        rail = email_receipt(service, 5, "Railco", "2026-02-12", 21.5)
        service.set_receipt_fields(rail, {"category": CATEGORY})
        assert by_id(service)[rail]["payment"] is None                     # 21 days: not offered
        service.set_payment(rail, "tx/1")
        payment = by_id(service)[rail]["payment"]
        assert payment["pinned"] and payment["far"] and payment["chips"][-1] == "+21 days"


def test_groups_and_reasons() -> None:
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        for v in ("railco", "cafe", "nowhere", "tipco"):
            service.db.set_state(f"confirmed_supplier:{v}", "1")
        waiting = email_receipt(service, 3, "Nowhere", "2099-01-01", 42.0)
        tip = email_receipt(service, 4, "Cafe", "2026-03-06", 7.5)         # £9 payment exists: a tip?
        for rid in (waiting, tip):
            service.set_receipt_fields(rid, {"category": CATEGORY})
        items = by_id(service)
        assert items[waiting]["group"] == "waiting" and items[waiting]["waiting_until"] == "2099-01-11"
        assert items[tip]["group"] == "needs" and items[tip]["reason"] == "No £7.50 payment"
        assert items[tip]["options"][0]["difference"] == 1.5
        service.set_payment(tip, items[tip]["options"][0]["url"])          # "Use this"
        item = by_id(service)[tip]
        assert item["payment"]["pinned"] and item["payment"]["chips"][0] == "+£1.50"
        assert item["payment"]["far"] is False                              # within the usual days
        assert item["will_file"]["vat"] == "£0 (none shown) + £1.50 tip (0%)"
        try:
            service.set_payment(tip, "https://elsewhere/tx/1")
        except ValueError:
            pass
        else:
            raise AssertionError("pinned a payment that isn't in the cache")


def test_the_statement_shows_which_payments_have_receipts() -> None:
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        email_receipt(service, 1, "Railco", "2026-03-04", 21.5)          # in Match
        service.mark_no_receipt("tx/3", "Tax payment")
        st = service.statement(None, "2026-03")
        status = {r["url"]: r["status"] for r in st["rows"]}
        assert status == {"tx/1": "in_match", "tx/2": "missing", "tx/3": "not_needed"}, status
        assert st["summary"]["with_receipt"] == 1 and st["summary"]["payments"] == 2
        assert st["summary"]["out"] == 39.5
        service.mark_no_receipt("tx/3", None)
        assert {r["url"]: r["status"] for r in service.statement(None, "2026-03")["rows"]}["tx/3"] == "missing"


def test_the_statement_can_show_every_month_newest_first() -> None:
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        month = service.statement(None, "2026-03")
        every = service.statement(None, "all")
        dates = [r["date"] for r in every["rows"]]
        assert dates == sorted(dates, reverse=True), "newest first"
        assert len(every["rows"]) == every["total_payments"] >= len(month["rows"]) == 3
        latest = service.statement(None, "all", limit=2)
        assert [r["url"] for r in latest["rows"]] == [r["url"] for r in every["rows"]][:2]
        assert latest["limit"] == 2 and latest["total_payments"] == every["total_payments"]
        page2 = service.statement(None, "all", limit=2, offset=2)               # Older ›
        assert [r["url"] for r in page2["rows"]] == [r["url"] for r in every["rows"]][2:4] and page2["offset"] == 2
        assert [r["date"] for r in month["rows"]] == sorted(r["date"] for r in month["rows"]), "a month: oldest first, as before"


def test_bank_feed_periods_are_months_and_accounting_years() -> None:
    """FreeAgent's menu: the months with payments, and the accounting years
    from the first year's end, newest first; a year shows its payments."""
    from app.service import _periods

    days = ["2024-05-02", "2025-03-30", "2025-04-02", "2026-02-10"]
    p = _periods(days, "2024-03-31")
    assert p["months"][-1] == {"period": "2024-05", "label": "May 2024"}
    # from the year with the first payment up to this one, newest first
    assert [y["label"] for y in p["years"]][-2:] == ["Accounting Year 2025/26", "Accounting Year 2024/25"]
    assert p["years"][-2]["period"] == "2025-04-01..2026-03-31"
    start, end = p["years"][0]["period"].split("..")
    assert start <= __import__("datetime").date.today().isoformat() <= end, "the newest is this year"
    assert _periods(days, "")["years"] == [], "no year end yet: months only"
    leap = _periods(["2024-02-29"], "2024-02-29")["years"]                     # a 29 February year end
    assert leap[-1]["period"] == "2023-03-01..2024-02-29" and leap[-2]["period"] == "2024-03-01..2025-02-28"
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        every = service.statement(None, "all")
        first = min(r["date"] for r in every["rows"])
        year = service.statement(None, f"{first}..{first}", limit=50)
        assert year["rows"] and all(r["date"] == first for r in year["rows"])


def test_a_linked_photo_on_the_statement_says_it_is_a_photo() -> None:
    """The panel showed a linked photo in a frame at full size: it didn't
    know it was a photo, so it couldn't scale it as Files does."""
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        photo = Path(tmp.name) / "receipt.jpg"
        photo.write_bytes(b"\xff\xd8\xff")
        rid = service.db.insert_receipt({
            "watcher_id": "photo", "source": "photo", "source_id": "p1", "vendor": "Sainsbury's",
            "purchased_on": "2026-03-06", "total": 9.0, "currency": "GBP", "pdf_path": str(photo)})
        service.db.update_receipt(rid, {"freeagent_json": json.dumps({"transaction": "tx/2"})})
        service.db.set_status(rid, "filed")
        row = next(r for r in service.statement(None, "2026-03")["rows"] if r["url"] == "tx/2")
        assert row["status"] == "filed"
        assert row["receipt"]["is_image"] is True and row["receipt"]["source"] == "photo"
        assert row["receipt"]["page_count"] == 0


def test_the_statement_lists_only_money_going_out() -> None:
    """A client paying an invoice needs no receipt; it only buried the
    payments that do."""
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        account = service.statement(None, "2026-03")["account"]
        service.db.save_bank_transactions([
            {"url": "tx/in", "bank_account": account, "dated_on": "2026-03-08", "amount": 1200.0,
             "unexplained_amount": 1200.0, "description": "CLIENT LTD INV 1042"}])
        st = service.statement(None, "2026-03")
        assert "tx/in" not in {r["url"] for r in st["rows"]}
        assert "in" not in st["summary"] and st["summary"]["out"] == 39.5


def test_use_this_on_an_undated_photo_links_it() -> None:
    """"Use this" did nothing on a photo with no date: the match needs one.
    It borrows the chosen payment's date, and gives it back on undo."""
    import json as _json
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        rid = service.db.insert_receipt({
            "watcher_id": "photo", "source": "photo", "source_id": "p1", "vendor": "Sainsbury's",
            "total": 8.5, "currency": "GBP", "paid_by": "business", "pdf_path": __file__,
            "extra_json": {"flags": ["No date found. Enter it from the receipt.", "Supplier name guessed by the Mac. Check it."]}})
        flags = lambda: _json.loads(service.db.get_receipt(rid)["extra_json"])["flags"]
        status = lambda: {r["url"]: r["status"] for r in service.statement(None, "2026-03")["rows"]}

        service.set_payment(rid, "tx/2")                          # £9.00 on 6 March
        assert service.db.get_receipt(rid)["purchased_on"] == "2026-03-06"
        assert status()["tx/2"] == "in_match"
        assert not any("No date" in f for f in flags()) and any("payment you chose" in f for f in flags())

        service.set_payment(rid, "tx/3")                          # changed your mind: 7 March
        assert service.db.get_receipt(rid)["purchased_on"] == "2026-03-07"

        service.set_payment(rid, None)                            # back to automatic
        assert service.db.get_receipt(rid)["purchased_on"] is None
        assert any("No date found" in f for f in flags()) and not any("payment you chose" in f for f in flags())

        service.set_receipt_fields(rid, {"purchased_on": "2026-03-05"})   # a date you typed stays
        service.set_payment(rid, "tx/2")
        assert service.db.get_receipt(rid)["purchased_on"] == "2026-03-05"


def test_a_payment_naming_the_guessed_supplier_confirms_it() -> None:
    """The Mac guessed "Sainsbury's"; the payment you chose says SAINSBURY'S.
    That settles "Check supplier", which otherwise kept Link hidden."""
    import json as _json
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        service.db.save_bank_transactions([
            {"url": "tx/s", "bank_account": "https://fa.test/v2/bank_accounts/1", "dated_on": "2026-03-09",
             "amount": -4.07, "unexplained_amount": -4.07, "description": "SAINSBURY'S // CARD_PURCHASE"}])
        rid = service.db.insert_receipt({
            "watcher_id": "photo", "source": "photo", "source_id": "p2", "vendor": "Sainsbury's",
            "purchased_on": "2026-03-09", "total": 4.07, "currency": "GBP", "paid_by": "business",
            "pdf_path": __file__, "extra_json": {"flags": ["Supplier name guessed by the Mac. Check it."]}})
        flags = lambda: _json.loads(service.db.get_receipt(rid)["extra_json"])["flags"]
        service.set_payment(rid, "tx/s")
        assert flags() == []
        service.set_payment(rid, "tx/2")                          # CAFE: doesn't name it
        assert flags() == ["Supplier name guessed by the Mac. Check it."]
        service.set_payment(rid, "tx/s")
        service.set_payment(rid, None)
        assert flags() == ["Supplier name guessed by the Mac. Check it."]


class GuessingFreeAgent:
    """FreeAgent with one payment it guessed (marked for approval). The API
    can't approve; deleting the guess leaves the payment unexplained."""
    connected = True

    def __init__(self, url, amount, guess, fail_create=False):
        from app.freeagent import WritesOff
        self.WritesOff = WritesOff
        self.writes_allowed = False
        self.url, self.amount, self.fail_create = url, amount, fail_create
        self.explanations = [guess]
        self.created, self.deleted = [], []

    def _write(self):
        if not self.writes_allowed:
            raise self.WritesOff("write")

    def get_url(self, url):
        explained = sum(float(e["gross_value"]) for e in self.explanations)
        return {"bank_transaction": {"url": self.url, "dated_on": "2026-03-10", "amount": str(self.amount),
                                     "unexplained_amount": str(self.amount - explained),
                                     "bank_transaction_explanations": list(self.explanations)}}

    def delete(self, url):
        self._write()
        self.deleted.append(url)
        self.explanations = [e for e in self.explanations if e["url"] != url]

    def create_explanation(self, body):
        self._write()
        if self.fail_create and "receipt_reference" in body:
            from app.freeagent import FreeAgentError
            raise FreeAgentError("FreeAgent said no (422)")
        made = {**body, "url": f"https://fa.test/v2/bank_transaction_explanations/{len(self.created) + 100}"}
        self.created.append(made)
        self.explanations.append(made)
        return made

    def add_explanation_attachments(self, url, attachments):
        self._write()
        return [{"url": "https://fa.test/v2/attachments/1"}]


def _toolco_receipt(service, base: Path) -> int:
    pdf = base / "toolco.pdf"
    pdf.write_bytes(b"%PDF-1.4 receipt")
    rid = email_receipt(service, 9, "Toolco", "2026-03-10", 12.0)
    service.db.update_receipt(rid, {"pdf_path": str(pdf)})
    return rid


def _guessed(service, fail_create=False):
    """A £12.00 payment FreeAgent guessed as software at 20%, not approved."""
    account = "https://fa.test/v2/bank_accounts/1"
    guess = {"url": "https://fa.test/v2/bank_transaction_explanations/50", "bank_transaction": "tx/g",
             "dated_on": "2026-03-10", "gross_value": "-12.0", "category": CATEGORY,
             "description": "TOOLCO", "sales_tax_rate": "20.0", "marked_for_review": True,
             "is_locked": False, "is_deletable": True}
    service.db.save_bank_transactions([{"url": "tx/g", "bank_account": account, "dated_on": "2026-03-10",
                                        "amount": -12.0, "unexplained_amount": 0, "description": "TOOLCO",
                                        "bank_transaction_explanations": [guess]}])
    client = GuessingFreeAgent("tx/g", -12.0, guess, fail_create)
    service._freeagent = lambda: client
    service._run_freeagent_sync = lambda: None
    return client


def test_approve_replaces_a_guess_with_an_approved_explanation() -> None:
    """FreeAgent's API can't approve a guess, so Approve removes it and
    makes the same explanation, which FreeAgent doesn't mark for approval."""
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        client = _guessed(service)
        service._run_approve("tx/g", None)
        assert client.deleted == ["https://fa.test/v2/bank_transaction_explanations/50"]
        made = client.created[0]
        assert (made["category"], made["sales_tax_rate"], made["gross_value"]) == (CATEGORY, "20.0", "-12.0")
        assert "marked_for_review" not in made
        assert service.db.get_state("replacing:tx/g") is None


def test_approve_sends_your_changes_to_a_payment_already_approved() -> None:
    """One button: Approve on an approved payment you changed updates
    FreeAgent's explanation."""
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        client = _guessed(service)
        tx = dict(service._cached_transaction("tx/g"))
        tx["bank_transaction_explanations"] = [{**client.explanations[0], "marked_for_review": False}]
        service.db.save_bank_transactions([tx])
        sent = []
        original = filer.update_existing_explanation
        filer.update_existing_explanation = lambda c, url, exp, changes: sent.append(changes) or {}
        try:
            service._run_approve("tx/g", None)
            assert sent == [], "nothing changed: nothing sent"
            service.set_payment_settings("tx/g", {"vat_rate": "0.0"})
            service._run_approve("tx/g", None)
        finally:
            filer.update_existing_explanation = original
        assert sent == [{"sales_tax_rate": "0.0"}]
        assert json.loads(service.db.get_state("explained:tx/g"))["state"] == "updated"


def test_approve_with_a_receipt_files_it_in_place_of_the_guess() -> None:
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        client = _guessed(service)
        rid = _toolco_receipt(service, Path(tmp.name))
        service._run_approve("tx/g", rid)
        assert client.deleted and client.created[0]["receipt_reference"] == f"RB-{rid}"
        assert client.created[0]["category"] == CATEGORY, "the guess's category, as you saw it"
        assert service.db.get_receipt(rid)["status"] == "filed"


def test_a_failed_approve_puts_freeagents_explanation_back() -> None:
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        client = _guessed(service, fail_create=True)
        rid = _toolco_receipt(service, Path(tmp.name))
        service._run_approve("tx/g", rid)
        assert client.deleted, "it got as far as removing the guess"
        assert len(client.explanations) == 1 and client.explanations[0]["category"] == CATEGORY
        assert client.explanations[0]["sales_tax_rate"] == "20.0"
        assert "put back" in service._file_results["results"][0]["note"]
        assert client.explanations[0]["gross_value"] == "-12.0"


def test_link_on_a_guess_approves_it_too() -> None:
    """Linking only attached the receipt to FreeAgent's guess, which stayed
    waiting for approval. Link replaces the guess, like Approve."""
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        client = _guessed(service)
        rid = _toolco_receipt(service, Path(tmp.name))
        assert by_id(service)[rid]["payment"]["explained"] is False, "a guess isn't attach-only"
        service._run_file([rid])
        assert client.deleted and client.created[0]["receipt_reference"] == f"RB-{rid}"


def test_a_transfer_is_never_replaced_by_a_receipt() -> None:
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        client = _guessed(service)
        client.explanations[0]["transfer_bank_account"] = "https://fa.test/v2/bank_accounts/2"
        rid = _toolco_receipt(service, Path(tmp.name))
        service._run_approve("tx/g", rid)
        assert client.deleted == [] and client.created == []


def test_remove_explanation_leaves_the_payment_unexplained() -> None:
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        client = _guessed(service)
        service._run_remove_explanation("tx/g")
        assert client.explanations == [] and client.created == []
        assert json.loads(service.db.get_state("removed:tx/g"))["explanation"]["category"] == CATEGORY


def test_a_locked_explanation_is_never_removed() -> None:
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        client = _guessed(service)
        client.explanations[0]["is_locked"] = True
        service._run_remove_explanation("tx/g")
        service._run_approve("tx/g", None)
        assert client.deleted == []


def test_remove_file_stops_it_being_suggested_for_that_payment() -> None:
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        rid = email_receipt(service, 1, "Railco", "2026-03-04", 21.5)
        status = lambda: {r["url"]: r["status"] for r in service.statement(None, "2026-03")["rows"]}
        assert status()["tx/1"] == "in_match"                 # suggested automatically
        service.remove_file_from_payment(rid, "tx/1")
        assert status()["tx/1"] == "missing"
        service.set_payment(rid, "tx/1")                       # chosen again: that wins
        assert status()["tx/1"] == "in_match"
        service.remove_file_from_payment(rid, "tx/1")          # and a choice can be removed
        assert status()["tx/1"] == "missing" and service.db.get_receipt(rid)["transaction_url"] is None


def test_a_receipt_waiting_too_long_is_overdue() -> None:
    service, _flagged, tmp = make()
    with tmp:
        connected(service, Path(tmp.name))
        old = service.db.insert_receipt({"watcher_id": "x", "gmail_message_id": "m9", "vendor": "Nowhere",
                                         "purchased_on": "2020-01-01", "total": 99.0, "currency": "GBP"})
        match = {r["id"]: r for r in service.receipts("pending")}[old]["match"]
        assert match["status"] == "waiting" and match["overdue"]
        service.set_waiting_days(90)
        try:
            service.set_waiting_days(0)
        except ValueError:
            pass
        else:
            raise AssertionError("accepted 0 days")


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
