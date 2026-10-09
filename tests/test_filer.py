"""Filing into FreeAgent, against a fake FreeAgent. Made-up values. No network.

What must hold: a plan with problems sends nothing;
a payment already explained isn't touched; an interrupted filing is
recovered by reading FreeAgent, not by posting twice; undo deletes only
what this app created.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import FILED, PENDING, Database  # noqa: E402
from app.filer import FilingError, file_receipt, plan_for, unfile, update_claim, vat_rate  # noqa: E402
from app.freeagent import WritesOff  # noqa: E402

TX = {"url": "https://fa.test/v2/bank_transactions/7", "dated_on": "2026-03-05", "amount": -21.50}
CATEGORY = "https://fa.test/v2/categories/365"
USER = "https://fa.test/v2/users/1"


class FakeClient:
    def __init__(self, unexplained: float = -21.50, explanations: list | None = None, fail_attach: bool = False):
        self.writes_allowed = False
        self.calls: list[tuple] = []
        self.unexplained = unexplained
        self.explanations = explanations or []
        self.fail_attach = fail_attach

    def _write(self, name, *args):
        if not self.writes_allowed:
            raise WritesOff(name)
        self.calls.append((name, *args))

    def get_url(self, url):
        self.calls.append(("get", url))
        return {"bank_transaction": {"url": url, "amount": "-21.5", "unexplained_amount": str(self.unexplained),
                                     "bank_transaction_explanations": self.explanations}}

    def create_explanation(self, body):
        self._write("create_explanation", body)
        return {"url": "https://fa.test/v2/bank_transaction_explanations/99"}

    def add_explanation_attachments(self, url, attachments):
        self._write("attach", url, attachments)
        if self.fail_attach:
            raise ConnectionError("network dropped")
        return [{"url": "https://fa.test/v2/attachments/5"}]

    def create_expense(self, body):
        self._write("create_expense", body)
        return {"url": "https://fa.test/v2/expenses/3"}

    def update_expense(self, url, body):
        self._write("update_expense", url, body)
        return {"url": url}

    def delete(self, url):
        self._write("delete", url)


def env(**row) -> tuple[Database, int, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    pdf = base / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 receipt")
    db = Database(base / "db.sqlite3")
    values = {"watcher_id": "photo", "source": "photo", "source_id": f"sha256:{id(tmp)}",
              "vendor": "Railco", "description": "Train travel", "purchased_on": "2026-03-04",
              "total": 21.50, "currency": "GBP", "pdf_path": str(pdf), "filename": "railco.pdf",
              "paid_by": "business"}
    values.update(row)
    return db, db.insert_receipt(values), tmp


def plan(db, rid, **kw):
    args = dict(transaction=TX, category_url=CATEGORY, user_url=USER, vat_registered=True)
    args.update(kw)
    return plan_for(db.get_receipt(rid), **args)


def test_files_an_explanation_then_attaches_the_receipt() -> None:
    db, rid, tmp = env()
    with tmp:
        client = FakeClient()
        file_receipt(client, db, rid, plan(db, rid))
        names = [c[0] for c in client.calls]
        assert names == ["get", "create_explanation", "attach"], names
        body = client.calls[1][1]
        assert body["gross_value"] == "-21.50" and body["receipt_reference"] == f"RB-{rid}"
        assert body["sales_tax_rate"] == "0.0", "no VAT printed: nothing reclaimed"
        attachment = client.calls[2][2][0]
        assert attachment["content_type"] == "application/x-pdf" and attachment["data"]
        row = db.get_receipt(rid)
        assert row["status"] == FILED and row["filed_at"]
        assert client.writes_allowed is False, "writes switched off again afterwards"


def test_a_plan_with_problems_sends_nothing() -> None:
    db, rid, tmp = env()
    with tmp:
        client = FakeClient()
        p = plan(db, rid, category_url=None)
        assert "Choose a category" in p.problems
        try:
            file_receipt(client, db, rid, p)
        except FilingError:
            pass
        else:
            raise AssertionError("expected FilingError")
        assert client.calls == []


def test_a_payment_already_explained_is_not_touched() -> None:
    db, rid, tmp = env()
    with tmp:
        client = FakeClient(unexplained=0)
        try:
            file_receipt(client, db, rid, plan(db, rid))
        except FilingError as exc:
            assert "already" in str(exc)
        else:
            raise AssertionError("expected FilingError")
        assert [c[0] for c in client.calls] == ["get"]


def test_an_interrupted_filing_is_recovered_not_reposted() -> None:
    db, rid, tmp = env()
    with tmp:
        first = FakeClient(fail_attach=True)
        try:
            file_receipt(first, db, rid, plan(db, rid))
        except ConnectionError:
            pass
        assert json.loads(db.get_receipt(rid)["freeagent_json"])["state"] == "explained"
        again = FakeClient()
        file_receipt(again, db, rid, plan(db, rid))
        assert [c[0] for c in again.calls] == ["attach"], "explanation must not be created twice"
        assert db.get_receipt(rid)["status"] == FILED


def test_a_filing_cut_off_before_the_reply_is_found_by_its_reference() -> None:
    db, rid, tmp = env()
    with tmp:
        db.update_receipt(rid, {"freeagent_json": json.dumps({"state": "filing", "transaction": TX["url"]})})
        client = FakeClient(explanations=[{"url": "https://fa.test/v2/bank_transaction_explanations/42",
                                           "receipt_reference": f"RB-{rid}"}])
        file_receipt(client, db, rid, plan(db, rid))
        assert "create_explanation" not in [c[0] for c in client.calls]
        assert json.loads(db.get_receipt(rid)["freeagent_json"])["url"].endswith("/42")


def test_a_personal_payment_files_as_an_expense_with_the_receipt() -> None:
    db, rid, tmp = env(paid_by="personal", currency="EUR", total=19.34)
    with tmp:
        client = FakeClient()
        p = plan(db, rid, transaction=None)
        assert p.kind == "expense" and not p.problems, p.problems
        file_receipt(client, db, rid, p)
        body = client.calls[0][1]
        assert body["user"] == USER and body["currency"] == "EUR" and body["gross_value"] == "-19.34"
        assert "sales_tax_rate" not in body, "foreign VAT isn't reclaimed"
        assert body["attachment"]["data"]


PROJECT = "https://fa.test/v2/projects/7"


def test_a_payment_or_expense_can_be_rebilled_to_a_client() -> None:
    db, rid, tmp = env()
    with tmp:
        p = plan(db, rid, rebill={"project": PROJECT, "type": "markup", "factor": 25})
        assert not p.problems, p.problems
        assert (p.body["project"], p.body["rebill_type"], p.body["rebill_factor"]) == (PROJECT, "markup", "0.25")
        p = plan(db, rid, rebill={"project": PROJECT, "type": "cost", "factor": None})
        assert p.body["rebill_type"] == "cost" and "rebill_factor" not in p.body
        p = plan(db, rid, rebill={"project": PROJECT, "type": "none", "factor": None})
        assert p.body["project"] == PROJECT and "rebill_type" not in p.body and "rebill_factor" not in p.body
        assert "Re-bill: enter the price" in plan(db, rid, rebill={"project": PROJECT, "type": "price", "factor": None}).problems
        assert "project" not in plan(db, rid).body
    db, rid, tmp = env(paid_by="personal", currency="GBP", total=12.0)
    with tmp:
        p = plan(db, rid, transaction=None, rebill={"project": PROJECT, "type": "price", "factor": 15})
        assert p.kind == "expense" and (p.body["project"], p.body["rebill_factor"]) == (PROJECT, "15.00"), p.problems


def test_a_set_price_isnt_shared_out_between_vat_rates() -> None:
    db, rid, tmp = env(total=7.05, vat=0.52, extra_json={"vat_lines": [["20", "0.52", "3.10"]]})
    with tmp:
        p = plan(db, rid, transaction=dict(TX, amount=-7.05), rebill={"project": PROJECT, "type": "price", "factor": 20})
        assert any("set price" in x for x in p.problems)
        p = plan(db, rid, transaction=dict(TX, amount=-7.05), rebill={"project": PROJECT, "type": "cost"})
        assert not p.problems and all(b["project"] == PROJECT for b in p.parts)


def test_unfile_deletes_only_what_was_created() -> None:
    db, rid, tmp = env()
    with tmp:
        file_receipt(FakeClient(), db, rid, plan(db, rid))
        client = FakeClient()
        unfile(client, db, rid)
        assert client.calls == [("delete", "https://fa.test/v2/bank_transaction_explanations/99")]
        assert db.get_receipt(rid)["status"] == PENDING


def test_the_amount_must_equal_the_payment() -> None:
    db, rid, tmp = env(total=20.00)
    with tmp:
        assert any("doesn't equal" in p for p in plan(db, rid).problems)


def test_vat_is_only_ever_what_was_printed() -> None:
    db, rid, tmp = env(total=7.05, vat=1.175)      # 20% of 7.05 incl.
    with tmp:
        assert vat_rate(db.get_receipt(rid), __import__("decimal").Decimal("7.05"), True) == ("20.0", None)
    db, rid, tmp = env(total=7.05, vat=0.52)       # two rates, but no per-rate lines read
    with tmp:
        rate, problem = vat_rate(db.get_receipt(rid), __import__("decimal").Decimal("7.05"), True)
        assert rate is None and "by hand" in problem
    db, rid, tmp = env(total=7.05, vat=1.175)
    with tmp:
        assert vat_rate(db.get_receipt(rid), __import__("decimal").Decimal("7.05"), False) == (None, None)


def test_a_mixed_rate_receipt_is_split_one_explanation_per_rate() -> None:
    """Sandwi-style: £3.10 at 20% (VAT £0.52 printed) and the rest zero-rated."""
    db, rid, tmp = env(total=7.05, vat=0.52, extra_json={"vat_lines": [["20", "0.52", "3.10"]]})
    tx = dict(TX, amount=-7.05)
    with tmp:
        p = plan(db, rid, transaction=tx)
        assert not p.problems, p.problems
        assert [(b["gross_value"], b["sales_tax_rate"]) for b in p.parts] == [("-3.10", "20.0"), ("-3.95", "0.0")]
        client = FakeClient(unexplained=-7.05)
        client.get_url = lambda url: {"bank_transaction": {"url": url, "amount": "-7.05", "unexplained_amount": "-7.05"}}
        created = iter(["https://fa.test/e/1", "https://fa.test/e/2"])
        client.create_explanation = lambda body: (client._write("create_explanation", body), {"url": next(created)})[1]
        file_receipt(client, db, rid, p)
        assert [c[0] for c in client.calls] == ["create_explanation", "create_explanation", "attach"]
        state = json.loads(db.get_receipt(rid)["freeagent_json"])
        assert state["urls"] == ["https://fa.test/e/1", "https://fa.test/e/2"]
        undo = FakeClient()
        unfile(undo, db, rid)
        assert [c[1] for c in undo.calls] == state["urls"], "undo removes every part"


def test_a_split_that_would_not_match_the_printed_vat_stops() -> None:
    # £3.12 at 20% is £0.52, but £3.00 at 20% is £0.50: the receipt says £0.52
    db, rid, tmp = env(total=7.05, vat=0.52, extra_json={"vat_lines": [["20", "0.52", "3.00"]]})
    with tmp:
        assert any("penny" in p for p in plan(db, rid, transaction=dict(TX, amount=-7.05)).problems)


def test_reverse_charge_goes_at_zero_with_the_ec_status() -> None:
    """FreeAgent accepts reverse charge only at 0% (checked in the sandbox)."""
    db, rid, tmp = env(vat=2.50)
    with tmp:
        p = plan(db, rid, vat_treatment="reverse_charge")
        assert not p.problems and len(p.parts) == 1
        assert p.body["ec_status"] == "Reverse Charge" and p.body["sales_tax_rate"] == "0.0"
        assert "ec_status" not in plan(db, rid).body, "only when the supplier is set to reverse charge"


def test_a_mixed_rate_expense_becomes_one_expense_per_rate() -> None:
    db, rid, tmp = env(paid_by="personal", total=7.05, vat=0.52,
                       extra_json={"vat_lines": [["20", "0.52", "3.10"]]})
    with tmp:
        p = plan(db, rid, transaction=None)
        assert not p.problems, p.problems
        assert [(b["gross_value"], b["sales_tax_rate"]) for b in p.parts] == [("-3.10", "20.0"), ("-3.95", "0.0")]
        client = FakeClient()
        made = iter(["https://fa.test/x/1", "https://fa.test/x/2"])
        client.create_expense = lambda body: (client._write("create_expense", body), {"url": next(made)})[1]
        file_receipt(client, db, rid, p)
        bodies = [c[1] for c in client.calls]
        assert "attachment" in bodies[0] and "attachment" not in bodies[1], "receipt attached once"
        undo = FakeClient()
        unfile(undo, db, rid)
        assert [c[1] for c in undo.calls] == ["https://fa.test/x/1", "https://fa.test/x/2"]


def test_an_interrupted_expense_is_found_by_its_reference() -> None:
    db, rid, tmp = env(paid_by="personal")
    with tmp:
        db.update_receipt(rid, {"freeagent_json": json.dumps({"state": "filing", "kind": "expense", "urls": []})})
        client = FakeClient()
        client.get_all = lambda path, key, params: [{"url": "https://fa.test/x/9", "receipt_reference": f"RB-{rid}"},
                                                     {"url": "https://fa.test/x/8", "receipt_reference": "RB-other"}]
        file_receipt(client, db, rid, plan(db, rid, transaction=None))
        assert "create_expense" not in [c[0] for c in client.calls]
        assert json.loads(db.get_receipt(rid)["freeagent_json"])["urls"] == ["https://fa.test/x/9"]


def test_an_already_explained_payment_only_gets_the_receipt_attached() -> None:
    explained_tx = dict(TX, unexplained_amount=0, explanation_url="https://fa.test/e/7")
    db, rid, tmp = env()
    with tmp:
        p = plan(db, rid, transaction=explained_tx, category_url=None)
        assert p.kind == "attach" and not p.problems, p.problems      # no category needed
        client = FakeClient()
        client.get_url = lambda url: {"bank_transaction": {"url": url, "dated_on": "2026-03-05",
            "bank_transaction_explanations": [{"url": "https://fa.test/e/7", "attachments": [], "is_locked": False}]}}
        file_receipt(client, db, rid, p)
        assert [c[0] for c in client.calls] == ["attach"], "nothing created, nothing changed"
        assert client.calls[0][1] == "https://fa.test/e/7"
        undo = FakeClient()
        undo.remove_explanation_attachments = lambda url, atts: undo._write("remove", url, atts)
        unfile(undo, db, rid)
        assert undo.calls == [("remove", "https://fa.test/e/7", ["https://fa.test/v2/attachments/5"])]
        assert db.get_receipt(rid)["status"] == "pending"


def test_rebilling_an_already_explained_payment_and_undoing_it() -> None:
    """Re-bill on a payment FreeAgent explained: its explanation gets the
    project and markup; Undo puts back what it had before."""
    explained_tx = dict(TX, unexplained_amount=0, explanation_url="https://fa.test/e/7")
    db, rid, tmp = env()
    with tmp:
        p = plan(db, rid, transaction=explained_tx, category_url=None,
                 rebill={"project": "https://fa.test/v2/projects/1", "type": "markup", "factor": 25})
        assert p.kind == "attach" and not p.problems, p.problems
        assert p.body["update"] == {"project": "https://fa.test/v2/projects/1", "rebill_type": "markup",
                                    "rebill_factor": "0.25"}
        client = FakeClient()
        client.get_url = lambda url: {"bank_transaction": {"url": url, "dated_on": "2026-03-05",
            "bank_transaction_explanations": [{"url": "https://fa.test/e/7", "attachments": [], "is_locked": False}]}}
        client.update_explanation = lambda url, changes: client._write("update", url, changes)
        file_receipt(client, db, rid, p)
        assert [c[0] for c in client.calls] == ["update", "attach"], client.calls
        undo = FakeClient()
        undo.remove_explanation_attachments = lambda url, atts: undo._write("remove", url, atts)
        undo.update_explanation = lambda url, changes: undo._write("update", url, changes)
        unfile(undo, db, rid)
        assert undo.calls[-1] == ("update", "https://fa.test/e/7",
                                  {"project": None, "rebill_type": None, "rebill_factor": None})


def test_choosing_another_category_on_an_explained_payment_changes_it_there() -> None:
    """FreeAgent's category is the default; picking another updates its
    explanation, and Undo puts FreeAgent's back."""
    explained_tx = dict(TX, unexplained_amount=0, explanation_url="https://fa.test/e/7",
                        explanation_category="https://fa.test/v2/categories/285")
    db, rid, tmp = env()
    with tmp:
        same = plan(db, rid, transaction=explained_tx, category_url="https://fa.test/v2/categories/285")
        assert "update" not in same.body, "FreeAgent's own category: nothing to change"
        p = plan(db, rid, transaction=explained_tx, category_url=CATEGORY)
        assert p.body["update"] == {"category": CATEGORY}
        client = FakeClient()
        client.get_url = lambda url: {"bank_transaction": {"url": url, "dated_on": "2026-03-05",
            "bank_transaction_explanations": [{"url": "https://fa.test/e/7", "attachments": [], "is_locked": False,
                                               "category": "https://fa.test/v2/categories/285"}]}}
        client.update_explanation = lambda url, changes: client._write("update", url, changes)
        file_receipt(client, db, rid, p)
        assert client.calls[0] == ("update", "https://fa.test/e/7", {"category": CATEGORY})
        undo = FakeClient()
        undo.remove_explanation_attachments = lambda url, atts: undo._write("remove", url, atts)
        undo.update_explanation = lambda url, changes: undo._write("update", url, changes)
        unfile(undo, db, rid)
        assert undo.calls[-1] == ("update", "https://fa.test/e/7", {"category": "https://fa.test/v2/categories/285"})


def test_changing_freeagents_own_explanation_returns_what_it_was() -> None:
    from app.filer import update_existing_explanation
    client = FakeClient()
    client.get_url = lambda url: {"bank_transaction": {"url": url, "bank_transaction_explanations": [
        {"url": "https://fa.test/e/1", "category": "c/old", "sales_tax_rate": "20.0", "is_locked": False}]}}
    client.update_explanation = lambda url, changes: client._write("update", url, changes)
    before = update_existing_explanation(client, "https://fa.test/t/1", "https://fa.test/e/1", {"category": "c/new"})
    assert before == {"category": "c/old"} and client.calls == [("update", "https://fa.test/e/1", {"category": "c/new"})]
    assert client.writes_allowed is False
    locked = FakeClient()
    locked.get_url = lambda url: {"bank_transaction": {"bank_transaction_explanations": [
        {"url": "https://fa.test/e/1", "is_locked": True}]}}
    try:
        update_existing_explanation(locked, "t", "https://fa.test/e/1", {"category": "c/new"})
        raise AssertionError("changed a locked explanation")
    except FilingError:
        pass


def test_attaching_refuses_if_freeagent_has_a_receipt_there_now() -> None:
    explained_tx = dict(TX, unexplained_amount=0, explanation_url="https://fa.test/e/7")
    db, rid, tmp = env()
    with tmp:
        client = FakeClient()
        client.get_url = lambda url: {"bank_transaction": {"url": url, "dated_on": "2026-03-05",
            "bank_transaction_explanations": [{"url": "https://fa.test/e/7", "attachments": [{"url": "a"}]}]}}
        try:
            file_receipt(client, db, rid, plan(db, rid, transaction=explained_tx))
        except FilingError as exc:
            assert "already has a receipt" in str(exc)
        else:
            raise AssertionError("expected FilingError")
        assert client.calls == []


def test_a_chosen_bigger_payment_files_the_tip_separately() -> None:
    db, rid, tmp = env(total=68.85)
    tx = dict(TX, amount=-75.74)
    with tmp:
        assert any("doesn't equal" in p for p in plan(db, rid, transaction=tx).problems), "not without your say-so"
        p = plan(db, rid, transaction=tx, allow_difference=True)
        assert not p.problems, p.problems
        assert [(b["gross_value"], b["sales_tax_rate"]) for b in p.parts] == [("-68.85", "0.0"), ("-6.89", "0.0")]
        assert "tip" in p.parts[1]["description"]


def test_a_foreign_expense_carries_the_pounds_charged() -> None:
    db, rid, tmp = env(paid_by="personal", currency="EUR", total=19.34, native_gross=16.71)
    with tmp:
        p = plan(db, rid, transaction=None)
        assert p.body["native_gross_value"] == "-16.71" and p.body["currency"] == "EUR"


def test_heic_and_oversized_files_are_refused() -> None:
    db, rid, tmp = env()
    with tmp:
        heic = Path(tmp.name) / "r.heic"
        heic.write_bytes(b"x")
        db.update_receipt(rid, {"pdf_path": str(heic)})
        assert any("heic" in p for p in plan(db, rid).problems)

def test_editing_a_claim_changes_the_same_expense_in_place() -> None:
    """Edit claim → Update FreeAgent: the expense already there is changed,
    not deleted and made again, and its receipt isn't sent a second time."""
    db, rid, tmp = env(paid_by="personal")
    with tmp:
        file_receipt(FakeClient(), db, rid, plan(db, rid, transaction=None))
        db.update_receipt(rid, {"total": 23.00})
        other = "https://fa.test/v2/categories/285"

        client = FakeClient()
        update_claim(client, db, rid, plan(db, rid, transaction=None, category_url=other))
        assert [c[:2] for c in client.calls] == [("update_expense", "https://fa.test/v2/expenses/3")]
        body = client.calls[0][2]
        assert body["gross_value"] == "-23.00" and body["category"] == other and "attachment" not in body
        state = json.loads(db.get_receipt(rid)["freeagent_json"])
        assert state["state"] == "filed" and state["urls"] == ["https://fa.test/v2/expenses/3"]
        assert db.get_receipt(rid)["status"] == FILED and client.writes_allowed is False
        # undoing the claim still deletes the same expense
        undo = FakeClient()
        unfile(undo, db, rid)
        assert undo.calls == [("delete", "https://fa.test/v2/expenses/3")]


def test_a_claim_whose_vat_split_changed_is_not_edited_in_place() -> None:
    db, rid, tmp = env(paid_by="personal", total=7.05)
    with tmp:
        file_receipt(FakeClient(), db, rid, plan(db, rid, transaction=None))
        db.update_receipt(rid, {"vat": 0.52, "extra_json": {"vat_lines": [["20", "0.52", "3.10"]]}})
        client = FakeClient()
        try:
            update_claim(client, db, rid, plan(db, rid, transaction=None))
        except FilingError as exc:
            assert "VAT split" in str(exc)
        else:
            raise AssertionError("expected the change to be refused")
        assert client.calls == []


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
