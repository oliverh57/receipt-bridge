"""The Statement's "Use that email": find an email receipt for a payment
that has none. Made-up emails and a fake Gmail; no network.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import email_finder  # noqa: E402
from app.config import Config  # noqa: E402
from app.email_message import Email  # noqa: E402
from app.service import ReceiptService  # noqa: E402

ACCOUNT = "https://fa.test/v2/bank_accounts/1"


def email(mid: str, subject: str, body: str, sender: str = "Example Shop <orders@example-shop.test>",
          day: str = "2026-09-27", pdf: bytes | None = None) -> Email:
    return Email(message_id=mid, thread_id=f"t-{mid}", subject=subject, sender=sender,
                 date=datetime.fromisoformat(day + "T10:00:00"), plain=body,
                 attachments=[{"filename": "receipt.pdf", "content_type": "application/pdf", "data": pdf}]
                 if pdf else [])


# ---- the pure part ------------------------------------------------------------


def test_the_search_covers_the_days_around_the_payment() -> None:
    q = email_finder.query(-23.99, "2026-09-28")
    assert q.startswith('"23.99" after:2026/09/21 before:2026/10/01')


def test_the_amount_must_be_shown_exactly_with_its_currency() -> None:
    assert email_finder.amount_in("Order total: £23.99", 23.99) == (True, True)
    assert email_finder.amount_in("You paid 23.99 GBP", 23.99) == (True, True)
    assert email_finder.amount_in("Item £23.99\nDelivery £0.00", 23.99) == (True, False)
    for text in ("Total £123.99", "Total 23.99", "Total $23.99", "Total £23.9", "Total £2,399.00"):
        assert email_finder.amount_in(text, 23.99)[0] is False, text


def test_emails_that_are_never_receipts_are_skipped() -> None:
    for subject in ("Your statement is ready", "Refund of £23.99", "Payment failed"):
        assert email_finder.judge(email("m", subject, "Total £23.99"), -23.99, "me@example.test") is None


def test_the_supplier_comes_from_the_sender() -> None:
    found = email_finder.judge(email("m", "Your order", "Order total £23.99"), -23.99, "me@example.test")
    assert found and found.supplier == "Example Shop" and found.as_total and found.day == "2026-09-27"


def test_a_total_beats_a_nearer_mention() -> None:
    near = email_finder.judge(email("a", "Basket", "Item £23.99", day="2026-09-28"), -23.99, "x")
    total = email_finder.judge(email("b", "Receipt", "Total £23.99", day="2026-09-25"), -23.99, "x")
    assert email_finder.best([near, total], "2026-09-28") is total


# ---- through the app, with a fake Gmail ------------------------------------------------


class FakeClient:
    def __init__(self, messages: list[Email]):
        self.messages = {m.message_id: m for m in messages}
        self.queries: list[str] = []

    def search(self, query: str, max_results: int = 100) -> list[dict]:
        self.queries.append(query)
        return [{"id": m, "threadId": f"t-{m}"} for m in self.messages][:max_results]

    def fetch(self, message_id: str, thread_id: str = "") -> Email:
        return self.messages[message_id]


class FakeAccount:
    email = "me@example.test"
    read_only = True

    def __init__(self, client: FakeClient):
        self._client = client

    def client(self, *_a, **_k) -> FakeClient:
        return self._client


def make(messages: list[Email]) -> tuple[ReceiptService, FakeClient, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    service = ReceiptService(Config(raw={
        "data_dir": str(base / "data"), "export_dir": str(base / "export"),
        "photo_inbox": str(base / "inbox"), "freeagent": {"credentials_file": str(base / "none.json")}}))
    service.db.set_state("freeagent:reference", json.dumps(
        {"bank_accounts": [{"url": ACCOUNT, "currency": "GBP", "name": "Business"}], "categories": []}))
    service.db.set_state("freeagent:accounts", json.dumps([ACCOUNT]))
    paid = (datetime.now() - timedelta(days=3)).date().isoformat()
    service.db.save_bank_transactions([
        {"url": "tx/amazon", "bank_account": ACCOUNT, "dated_on": paid, "amount": -23.99,
         "unexplained_amount": -23.99, "description": "AMAZON* MKTPLACE"}])
    client = FakeClient(messages)
    account = FakeAccount(client)
    service.accounts.list = lambda: [account]              # type: ignore[method-assign]
    service.accounts.get = lambda e: account if e == account.email else None   # type: ignore[method-assign]
    return service, client, tmp


def missing_row(service: ReceiptService) -> dict:
    sheet = service.statement(None, (datetime.now() - timedelta(days=3)).strftime("%Y-%m"))
    return next(r for r in sheet["rows"] if r["url"] == "tx/amazon")


def test_an_email_is_found_offered_and_used() -> None:
    day = (datetime.now() - timedelta(days=4)).date().isoformat()
    order = email("amz1", "Your order", "Order total: £23.99", day=day, pdf=b"%PDF-1.4 order")
    service, client, tmp = make([email("news", "Deals this week", "Gadgets from £23.99!", day=day), order])
    with tmp:
        assert missing_row(service)["suggestion"] is None
        service._run_find_emails()
        assert len(client.queries) == 1 and '"23.99"' in client.queries[0]
        row = missing_row(service)
        assert row["status"] == "missing"
        assert row["suggestion"]["kind"] == "email" and row["suggestion"]["message_id"] == "amz1"
        assert row["suggestion"]["label"].startswith("Found an Example Shop email for £23.99 on ")

        service._run_find_emails()                          # found already: no second search
        assert len(client.queries) == 1

        service._run_use_suggestion("tx/amazon")
        receipt = next(r for r in service.receipts("pending") if r["supplier"] == "Example Shop")
        assert receipt["total"] == 23.99 and receipt["pinned_payment"] == "tx/amazon"
        assert receipt["payment"]["description"] == "AMAZON* MKTPLACE"
        assert missing_row(service)["status"] == "in_match"
        assert service.snapshot()["outcome"]["ok"] is True


def test_nothing_found_is_looked_for_again_tomorrow_not_now() -> None:
    service, client, tmp = make([email("x", "Hello", "No money here")])
    with tmp:
        service._run_find_emails()
        service._run_find_emails()
        assert len(client.queries) == 1
        assert missing_row(service)["suggestion"] is None


def test_an_ignored_receipt_with_the_same_amount_is_offered_without_gmail() -> None:
    service, client, tmp = make([])
    with tmp:
        day = (datetime.now() - timedelta(days=4)).date().isoformat()
        rid = service.db.insert_receipt({"watcher_id": "shop", "gmail_message_id": f"m{time.time_ns()}",
                                         "vendor": "Example Shop", "purchased_on": day, "total": 23.99,
                                         "currency": "GBP", "status": "ignored"})
        suggestion = missing_row(service)["suggestion"]
        assert suggestion["kind"] == "ignored" and suggestion["id"] == rid
        service._run_use_suggestion("tx/amazon")
        assert service.db.get_receipt(rid)["status"] == "pending"
        assert service.db.get_receipt(rid)["transaction_url"] == "tx/amazon"
        assert client.queries == []


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
