"""The Emails view: browse the mailbox, spot the receipts, add any one email
to Files. Made-up emails and a fake Gmail; no network, no browser.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import email_inbox  # noqa: E402
from app.api import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.email_message import Email  # noqa: E402
from app.service import ReceiptService  # noqa: E402

ME = "me@example.test"


def email(mid: str, subject: str, body: str, sender: str = "Example Hotel <billing@example-hotel.test>",
          pdf: bytes | None = None) -> Email:
    return Email(message_id=mid, thread_id=f"t-{mid}", subject=subject, sender=sender,
                 date=datetime.fromisoformat("2026-09-27T10:00:00+00:00"), plain=body,
                 attachments=[{"filename": "invoice.pdf", "content_type": "application/pdf", "size": len(pdf),
                               "data": pdf}] if pdf else [])


def header(mid: str, subject: str, snippet: str, sender: str = "Example Hotel <billing@example-hotel.test>",
           labels: list[str] | None = None, mixed: bool = False) -> dict:
    return {"id": mid, "thread_id": f"t-{mid}", "subject": subject, "sender": sender, "date": "",
            "snippet": snippet, "label_ids": labels or ["INBOX"], "internal_date": "2026-09-27T10:00:00+00:00",
            "has_attachment": mixed}


# ---- spotting receipts from the list alone ----------------------------------------


def test_receipts_are_picked_out_from_their_subject_sender_and_snippet() -> None:
    likely = [
        ("Your receipt from Example Hotel", "Thanks for staying. Total paid: £184.00", "Example Hotel <billing@example-hotel.test>"),
        ("Order confirmation #40211", "Order number 40211. Order total £23.99", "Shop <orders@shop.test>"),
        ("Invoice INV-0042", "Please find attached your invoice. Amount paid £60.00 VAT £10.00", "Acme <hello@acme.test>"),
        ("Your Uber trip", "Total £12.40 Thanks for riding", "Uber Receipts <noreply@uber.com>"),
    ]
    for subject, snippet, sender in likely:
        assert email_inbox.judge(subject, sender, snippet).level == "likely", subject


def test_newsletters_sales_and_sign_in_emails_are_not_receipts() -> None:
    for subject, snippet, labels in [
        ("Our autumn sale: 30% off everything", "Coats from £49.99", ["CATEGORY_PROMOTIONS"]),
        ("Your statement is ready", "Your balance is £1,203.11", []),
        ("Reset your password", "Use this code to sign in", []),
        ("Lunch on Friday?", "Are you free at 1?", []),
        ("Weekly newsletter", "Top stories this week", ["CATEGORY_PROMOTIONS"]),
        ("[you/receipt-bridge] Possible valid secrets bypassed", "Secrets bypassed push protection", []),
        ("[you/receipt-bridge] Google OAuth2 Keys exposed on GitHub", "GitGuardian has detected keys", []),
        ("Re: receipt-bridge release notes", "Shipping 1.1.6 today", []),
    ]:
        assert email_inbox.judge(subject, "Shop <news@shop.test>", snippet, labels).level is None, subject


def test_a_hint_of_a_receipt_is_a_maybe_and_says_why() -> None:
    signal = email_inbox.judge("Thanks!", "Shop <hello@shop.test>", "You paid £5.00 today")
    assert signal.level == "maybe"
    assert signal.reasons and all(isinstance(r, str) and r for r in signal.reasons)


def test_a_list_line_shows_the_amount_and_unescapes_gmails_snippet() -> None:
    row = email_inbox.list_row(header("m1", "Your receipt", "Rock &amp; Roll Ltd &mdash; Total: &pound;9.50"), ME)
    assert row["snippet"] == "Rock & Roll Ltd — Total: £9.50"
    assert row["amount"] == {"amount": 9.5, "currency": "GBP", "as_total": True}
    assert row["receipt"] == "likely" and row["account"] == ME
    assert row["from_name"] == "Example Hotel" and row["from_address"] == "billing@example-hotel.test"


def test_the_list_query_leaves_out_sent_drafts_and_bins_and_can_look_for_receipts() -> None:
    assert email_inbox.list_query() == email_inbox.BASE_QUERY
    q = email_inbox.list_query("from:hotel", receipts_only=True)
    assert q.startswith("from:hotel {receipt invoice") and q.endswith(email_inbox.BASE_QUERY)


# ---- what an opened email would become -----------------------------------------------


def test_the_draft_reads_supplier_date_total_and_vat() -> None:
    message = email("m1", "Your receipt", "Room £150.00\nBreakfast £30.00\nVAT £30.00\nTotal paid: £180.00")
    draft = email_inbox.draft(message)
    assert draft["supplier"] == "Example Hotel" and draft["date"] == "2026-09-27"
    assert (draft["total"], draft["currency"], draft["vat"]) == (180.0, "GBP", 30.0)
    assert draft["total_found_in"] == "the email" and draft["pdf"] == ""
    assert {a["amount"] for a in draft["amounts"]} >= {180.0, 150.0}


def test_a_sumup_tax_table_gives_its_tax_not_its_net() -> None:
    """SumUp/Square: "A (20%) VAT £36.75 £7.35 £44.10" is net, tax, total.
    The VAT is £7.35, whether the row is one line or a cell a line; never
    more than 20% of the total."""
    sender = "Mikkeller Bar London <no-reply@sumup.com>"
    items = "Baddest Behaviour x 3 £23.40\nLuke's Cider - Luke's Gospel £6.90\nTotal £44.10\n"
    one_line = items + "Tax rate Tax Name Net Tax Total\nA (20%) VAT £36.75 £7.35 £44.10"
    cells = items + "\n".join(["Tax rate", "Tax Name", "Net", "Tax", "Total", "A (20%)", "VAT", "£36.75", "£7.35", "£44.10"])
    for body in (one_line, cells):
        draft = email_inbox.draft(email("m9", "Receipt from Mikkeller Bar London", body, sender=sender))
        assert (draft["total"], draft["vat"]) == (44.10, 7.35), (body, draft["vat"])
    unlabelled = email_inbox.draft(email("m8", "Receipt", "Total £44.10\nVAT £36.75", sender=sender))
    assert unlabelled["vat"] is None, "more than 20% of the total is never VAT"


def test_pictures_inside_the_email_are_shown_in_place() -> None:
    raw = (b'From: Shop <a@shop.test>\nSubject: Receipt\nMIME-Version: 1.0\n'
           b'Content-Type: multipart/related; boundary="X"\n\n--X\nContent-Type: text/html\n\n'
           b'<img src="cid:logo@shop"><img src="cid:gone">\n--X\nContent-Type: image/png\n'
           b'Content-ID: <logo@shop>\nContent-Transfer-Encoding: base64\n\niVBORw0KGgo=\n--X--\n')
    message = Email.from_bytes(raw)
    assert message.attachments == [], "a picture in the email isn't an attachment"
    html = message.html_with_images()
    assert '<img src="data:image/png;base64,iVBORw0KGgo=">' in html and 'src="cid:gone"' in html
    assert message.html_with_images(limit=1) == message.html, "too big: left as it was"


def test_nothing_is_guessed_when_the_email_shows_no_money() -> None:
    draft = email_inbox.draft(email("m1", "Hello", "See you soon", sender="friend@example.test"))
    assert draft["total"] is None and draft["vat"] is None and draft["currency"] == "GBP"
    assert draft["supplier"] == "Example"


# ---- the service, with a fake Gmail -------------------------------------------------


class FakeClient:
    def __init__(self, messages: dict[str, Email], headers: list[dict]):
        self.messages, self._headers = messages, headers
        self.queries: list[tuple[str, str]] = []
        self.fetched: list[str] = []

    def search_page(self, query: str, page_token: str = "", max_results: int = 50):
        self.queries.append((query, page_token))
        stubs = [{"id": h["id"], "threadId": h["thread_id"]} for h in self._headers]
        return (stubs[:2], "page2") if not page_token else (stubs[2:], "")

    def headers(self, ids: list[str]) -> list[dict]:
        return [h for h in self._headers if h["id"] in ids]

    def fetch(self, message_id: str, thread_id: str = "") -> Email:
        self.fetched.append(message_id)
        return self.messages[message_id]


class FakeAccount:
    email = ME
    read_only = True

    def __init__(self, client: FakeClient):
        self._client = client

    def client(self, *_a, **_k) -> FakeClient:
        return self._client


def make() -> tuple[ReceiptService, FakeClient, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    service = ReceiptService(Config(raw={
        "data_dir": str(base / "data"), "export_dir": str(base / "export"),
        "photo_inbox": str(base / "inbox"), "freeagent": {"credentials_file": str(base / "none.json")}}))
    messages = {
        "hotel1": email("hotel1", "Your receipt", "VAT £30.00\nTotal paid: £180.00", pdf=b"%PDF-1.4 hotel"),
        "lunch1": email("lunch1", "Lunch?", "Free on Friday?", sender="Sam <sam@example.test>"),
        "shop01": email("shop01", "Order confirmation", "Order total £23.99", sender="Shop <orders@shop.test>",
                        pdf=b"%PDF-1.4 shop"),
    }
    client = FakeClient(messages, [
        header("hotel1", "Your receipt", "Total paid: £180.00"),
        header("lunch1", "Lunch?", "Free on Friday?", sender="Sam <sam@example.test>"),
        header("shop01", "Order confirmation", "Order total £23.99", sender="Shop <orders@shop.test>"),
    ])
    account = FakeAccount(client)
    service.accounts.list = lambda: [account]              # type: ignore[method-assign]
    service.accounts.get = lambda e: account if e == account.email else None   # type: ignore[method-assign]
    return service, client, tmp


def run_queued(service: ReceiptService, key: str) -> None:
    """The worker isn't started in tests: run the job that was queued."""
    queued, job = service._jobs.get_nowait()
    assert queued == key, queued
    job()
    service._queued.discard(key)


def test_the_list_comes_a_page_at_a_time_with_receipts_marked() -> None:
    service, client, tmp = make()
    with tmp:
        page = service.list_emails(search="hotel")
        assert page["account"] == ME and page["next"] == "page2"
        assert [r["id"] for r in page["emails"]] == ["hotel1", "lunch1"]
        assert [r["receipt"] for r in page["emails"]] == ["likely", None]
        assert client.queries[0][0].startswith("hotel ")
        more = service.list_emails(search="hotel", page_token=page["next"])
        assert [r["id"] for r in more["emails"]] == ["shop01"] and more["next"] == ""


def test_likely_receipts_shows_only_what_the_app_judges_a_receipt() -> None:
    service, client, tmp = make()
    with tmp:
        page = service.list_emails(receipts_only=True)
        # Gmail's word search returned all three; lunch isn't a receipt. The
        # thinned first page read on into the next.
        assert [r["id"] for r in page["emails"]] == ["hotel1", "shop01"] and page["next"] == ""
        assert len(client.queries) == 2 and "{receipt" in client.queries[0][0]
        assert [r["id"] for r in service.list_emails()["emails"]] == ["hotel1", "lunch1"], "All mail is all mail"


def test_an_email_a_supplier_rule_will_collect_is_marked_until_its_scan_has_been() -> None:
    service, _, tmp = make()
    with tmp:
        rules = Path(tmp.name) / "watchers"
        rules.mkdir()
        service.config.raw["watchers_dir"] = str(rules)
        (rules / "shop.yaml").write_text(
            "id: shop\nname: Shop\ngmail_query: from:shop.test\n"
            "match:\n  from_contains: shop.test\n  body_contains: Order number\n"      # the body: left to the scan
            "  exclude_if_contains: [refunded]\n", encoding="utf-8")
        (rules / "_example.yaml").write_text("id: example\ngmail_query: x\nmatch:\n  from_contains: example-hotel\n",
                                             encoding="utf-8")
        service.db.set_state(f"scan_from:{ME}", "2026-09-01")
        rule = {r["id"]: r["rule"] for r in service.list_emails()["emails"]} | \
            {r["id"]: r["rule"] for r in service.list_emails(page_token="page2")["emails"]}
        assert rule == {"hotel1": None, "lunch1": None, "shop01": {"id": "shop", "name": "Shop"}}, rule

        line = {"id": "shop01", "date": "2026-09-27T10:00:00+00:00", "from_name": "Shop",
                "from_address": "orders@shop.test", "subject": "Order confirmation", "snippet": "Order total £23.99"}
        assert service.email_rules(ME, [{**line, "snippet": "Your order was refunded"}]) == {"shop01": None}
        service.db.set_state(f"scan_from:{ME}", "2026-09-28")
        assert service.email_rules(ME, [line]) == {"shop01": None}, "before the rule's scan reaches"
        service.db.set_state(f"scan_from:{ME}", "2026-09-01")
        service.db.set_state(f"last_scan:{ME}:shop", "2026-09-27")
        assert service.email_rules(ME, [line])["shop01"], "scanned that day: it may still come"
        service.db.set_state(f"last_scan:{ME}:shop", "2026-09-28")
        assert service.email_rules(ME, [line]) == {"shop01": None}, "scanned since and not taken: yours to check"


def test_an_email_is_added_to_files_as_an_expense_with_what_you_typed() -> None:
    service, client, tmp = make()
    with tmp:
        opened = service.open_email("", "hotel1")
        assert opened["draft"]["total"] == 180.0 and opened["in_files"] is None
        fields = {"supplier": "The Example Hotel", "date": "2026-09-26", "total": "180.00",
                  "currency": "gbp", "vat": "30", "paid_by": "personal"}
        assert service.add_email(ME, "hotel1", fields) is True
        assert service.add_email(ME, "hotel1", fields) is False, "already queued"
        run_queued(service, "email-add:hotel1")
        assert client.fetched == ["hotel1"], "opened once, kept for adding"
        known = service.emails_in_files(["hotel1", "lunch1"])
        assert list(known) == ["hotel1"] and known["hotel1"]["status"] == "pending"
        receipt = next(r for r in service.receipts("pending") if r["id"] == known["hotel1"]["id"])
        assert receipt["supplier"] == "The Example Hotel" and receipt["date"] == "2026-09-26"
        assert (receipt["total"], receipt["vat"], receipt["paid_by"]) == (180.0, 30.0, "personal")
        assert receipt["document"] == "supplier" and receipt["has_pdf"], "the attached PDF is the receipt"
        assert receipt["filename"] == "2026-09-26 The Example Hotel GBP180.00.pdf"
        assert service.snapshot()["outcome"]["message"] == "Added the The Example Hotel email to Files as an expense."
        assert service.list_emails()["emails"][0]["in_files"]["status"] == "pending"
        try:
            service.add_email(ME, "hotel1", fields)
            raise AssertionError("added twice")
        except ValueError as exc:
            assert "already" in str(exc)


def test_the_vat_rate_chosen_is_how_it_will_be_filed() -> None:
    service, _, tmp = make()
    with tmp:
        service.add_email(ME, "hotel1", {"supplier": "Hotel", "total": "180", "vat": "30", "vat_choice": "20.0"})
        run_queued(service, "email-add:hotel1")
        service.add_email(ME, "shop01", {"supplier": "Shop", "total": "23.99", "vat": "2.00", "vat_choice": "amount"})
        run_queued(service, "email-add:shop01")
        rows = {r["supplier"]: r for r in service.receipts("pending")}
        assert rows["Hotel"]["vat_choice"] == "20.0" and rows["Hotel"]["vat"] is None
        assert rows["Shop"]["vat_choice"] == "amount" and rows["Shop"]["vat_amount"] == 2.0
        assert rows["Shop"]["vat"] == 2.0


def test_an_email_you_ignored_comes_back_when_you_add_it() -> None:
    service, _, tmp = make()
    with tmp:
        rid = service.db.insert_receipt({"watcher_id": "shop", "gmail_message_id": "shop01", "vendor": "Shop",
                                         "total": 23.99, "currency": "GBP", "status": "ignored"})
        assert service.add_email(ME, "shop01", {"supplier": "Shop", "total": "23.99"})
        run_queued(service, "email-add:shop01")
        row = service.db.get_receipt(rid)
        assert row["status"] == "pending" and row["pdf_path"] and row["purchased_on"] == "2026-09-27"


def test_a_supplier_name_cant_steer_where_the_file_is_exported() -> None:
    service, _, tmp = make()
    with tmp:
        service.add_email(ME, "shop01", {"supplier": "../../Shop/x", "total": "23.99"})
        run_queued(service, "email-add:shop01")
        row = service.db.list_receipts("pending")[0]
        assert "/" not in row["filename"] and not row["filename"].startswith("."), row["filename"]


def test_the_form_is_checked_before_anything_is_queued() -> None:
    service, _, tmp = make()
    with tmp:
        for fields, words in [
            ({"date": "27/09/2026"}, "date"),
            ({"total": "lots"}, "total should be a number"),
            ({"total": "-3"}, "negative"),
            ({"total": "10", "vat": "12"}, "less than the total"),
            ({"currency": "pounds"}, "three letters"),
            ({"paid_by": "someone"}, "business or personal"),
            ({"vat_choice": "17.5"}, "VAT rate from the list"),
            ({"total": "120", "vat": "25", "vat_choice": "amount"}, "more than 20% VAT"),
        ]:
            try:
                service.add_email(ME, "hotel1", fields)
                raise AssertionError(f"accepted {fields}")
            except ValueError as exc:
                assert words in str(exc), (fields, exc)
        assert not any(k.startswith("email-add") for k in service.snapshot()["queued"])


def test_signing_out_forgets_its_opened_emails() -> None:
    service, _, tmp = make()
    with tmp:
        service.open_email(ME, "hotel1")
        assert service._email_cache
        service.accounts.remove = lambda email, revoke=True: True   # type: ignore[method-assign]
        service.disconnect_account(ME)
        assert not service._email_cache


# ---- the API ----------------------------------------------------------------------


def test_the_api_needs_the_token_and_a_real_message_id() -> None:
    service, _, tmp = make()
    with tmp:
        app = create_app(service)
        client, token = TestClient(app, base_url="http://127.0.0.1"), app.state.token
        assert client.get("/api/emails").status_code == 403
        listed = client.get("/api/emails?q=hotel", headers={"x-receipt-bridge": token})
        assert listed.status_code == 200 and listed.json()["emails"][0]["id"] == "hotel1"
        assert client.get("/api/emails/..%2Fsecret", headers={"x-receipt-bridge": token}).status_code in (400, 404)
        assert client.get("/api/emails/a<b>cdef", headers={"x-receipt-bridge": token}).status_code == 400
        opened = client.get("/api/emails/hotel1", headers={"x-receipt-bridge": token}).json()
        assert opened["subject"] == "Your receipt" and opened["attachments"][0]["filename"] == "invoice.pdf"
        assert "data" not in opened["attachments"][0], "attachment bytes stay on the server"
        pdf = client.get(f"/api/emails/hotel1/pdf?t={token}")
        assert pdf.status_code == 200 and pdf.content == b"%PDF-1.4 hotel"
        assert pdf.headers["content-type"] == "application/pdf"
        assert client.get(f"/api/emails/lunch1/pdf?t={token}").status_code == 404
        assert client.get("/api/emails/hotel1/pdf").status_code == 403
        bad = client.post("/api/emails/hotel1/add", json={"total": "abc"}, headers={"x-receipt-bridge": token})
        assert bad.status_code == 400 and "number" in bad.json()["detail"]
        known = client.post("/api/emails/known", json={"ids": ["hotel1"]}, headers={"x-receipt-bridge": token})
        assert known.json() == {}


def test_signed_out_the_list_says_to_connect_gmail() -> None:
    service, _, tmp = make()
    with tmp:
        service.accounts.list = lambda: []                     # type: ignore[method-assign]
        service.accounts.get = lambda e: None                  # type: ignore[method-assign]
        app = create_app(service)
        client, token = TestClient(app, base_url="http://127.0.0.1"), app.state.token
        res = client.get("/api/emails", headers={"x-receipt-bridge": token})
        assert res.status_code == 400 and "Connect a Gmail account" in res.json()["detail"]


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
