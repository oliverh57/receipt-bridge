"""Matching receipts to bank transactions. Made-up suppliers and amounts,
shaped like a real feed: one fare repeated most days, settling a day or two
after the receipt, plus a few one-offs. No network.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.matcher import match_receipts, supplier_token, why_not_open  # noqa: E402

GBP = {"GBP"}


def tx(n: int, when: str, amount: float, description: str, unexplained: float | None = None) -> dict:
    return {"url": f"tx/{n}", "dated_on": when, "amount": amount, "description": description,
            "unexplained_amount": amount if unexplained is None else unexplained}


def rc(n: int, when: str, total: float, vendor: str, paid_by: str | None = "business",
       currency: str = "GBP") -> dict:
    return {"id": n, "date": when, "total": total, "vendor": vendor, "paid_by": paid_by,
            "currency": currency}


FEED = [
    tx(1, "2026-03-02", -21.50, "RAILCO.COM"),
    tx(2, "2026-03-05", -21.50, "RAILCO.COM"),
    tx(3, "2026-03-05", -21.50, "RAILCO.COM"),
    tx(4, "2026-03-06", -21.50, "RAILCO.COM"),
    tx(5, "2026-03-06", -9.00, "SOFTWARECO"),
    tx(6, "2026-03-07", -9.00, "CAFE NORTH"),
    tx(7, "2026-03-08", 1500.00, "CLIENT PAYMENT"),
    tx(8, "2026-03-09", -12.00, "TAXICO *TRIP", unexplained=0),     # already explained
]


def test_a_single_exact_payment_is_matched() -> None:
    result = match_receipts([rc(1, "2026-03-01", 21.50, "Railco")], FEED[:1], GBP)[1]
    assert result.status == "matched" and result.transaction["url"] == "tx/1" and result.alias_hit


def test_repeated_fares_pair_in_date_order() -> None:
    """Receipts on the 3rd and 4th both settle on the 5th; the 5th's on the 6th."""
    receipts = [rc(1, "2026-03-01", 21.50, "Railco"), rc(2, "2026-03-03", 21.50, "Railco"),
                rc(3, "2026-03-04", 21.50, "Railco"), rc(4, "2026-03-05", 21.50, "Railco")]
    result = match_receipts(receipts, FEED, GBP)
    assert [result[i].transaction["url"] for i in (1, 2, 3, 4)] == ["tx/1", "tx/2", "tx/3", "tx/4"]
    assert result[2].status == "matched", "identical payments: take the nearest, never ask (§11)"



def test_a_receipt_tied_to_one_account_only_matches_its_payments() -> None:
    """A supplier rule that says "paid with the other card": the same amount
    on the main account isn't a match."""
    feed = [dict(tx(1, "2026-03-02", -21.50, "RAILCO.COM"), bank_account="acct/main"),
            dict(tx(2, "2026-03-03", -21.50, "RAILCO.COM"), bank_account="acct/card")]
    tied = dict(rc(1, "2026-03-01", 21.50, "Railco"), account="acct/card")
    assert match_receipts([tied], feed, GBP)[1].transaction["url"] == "tx/2"
    assert match_receipts([dict(tied, account="acct/other")], feed, GBP)[1].status == "waiting"


def test_the_supplier_name_beats_a_same_amount_payment_elsewhere() -> None:
    result = match_receipts([rc(1, "2026-03-06", 9.00, "Cafe North Ltd")], FEED, GBP)[1]
    assert result.transaction["url"] == "tx/6" and result.status == "matched"


def test_no_payment_yet_waits() -> None:
    result = match_receipts([rc(1, "2026-03-20", 21.50, "Railco")], FEED, GBP)[1]
    assert result.status == "waiting"


def test_explained_transactions_and_income_are_never_candidates() -> None:
    result = match_receipts([rc(1, "2026-03-09", 12.00, "Taxico")], FEED, GBP)[1]
    assert result.status == "waiting"
    assert match_receipts([rc(2, "2026-03-08", -1500.00, "Client")], FEED, GBP)[2].status == "waiting"


def test_outside_the_window_is_not_a_match() -> None:
    # tx/1 is 9 days after: beyond the 7-day window
    assert match_receipts([rc(1, "2026-02-21", 21.50, "Railco")], FEED[:1], GBP)[1].status == "waiting"


def test_an_expense_with_an_exact_business_payment_is_flagged() -> None:
    result = match_receipts([rc(1, "2026-03-05", 9.00, "Softwareco", paid_by="personal")], FEED, GBP)[1]
    assert result.status == "expense_but_found"
    plain = match_receipts([rc(2, "2026-03-05", 4.20, "Bakery", paid_by="personal")], FEED, GBP)[2]
    assert plain.status == "not_applicable"


def test_foreign_currency_and_missing_totals_are_not_matched() -> None:
    result = match_receipts([rc(1, "2026-03-05", 21.50, "Railco", currency="EUR"),
                             {"id": 2, "date": None, "total": 5, "currency": "GBP"}], FEED, GBP)
    assert result[1].status == result[2].status == "not_applicable"


def test_every_payment_goes_to_one_receipt_only() -> None:
    receipts = [rc(i, "2026-03-04", 21.50, "Railco") for i in range(1, 7)]
    result = match_receipts(receipts, FEED, GBP)
    urls = [m.transaction["url"] for m in result.values() if m.transaction]
    assert len(urls) == len(set(urls)) == 4          # four £21.50s in the feed
    assert sum(m.status == "waiting" for m in result.values()) == 2


def test_a_payment_freeagent_already_explained_without_a_receipt_is_a_candidate() -> None:
    explained = dict(tx(9, "2026-03-10", -14.00, "BOOKSHOP", unexplained=0),
                     explanation_url="e/1", explanation_attachments=0, explanation_locked=0)
    with_receipt = dict(explained, url="tx/10", explanation_attachments=1)
    locked = dict(explained, url="tx/11", explanation_locked=1)
    result = match_receipts([rc(1, "2026-03-09", 14.00, "Bookshop")], [explained], GBP)[1]
    assert result.status == "matched" and result.transaction["url"] == "tx/9"
    for other in (with_receipt, locked):
        assert match_receipts([rc(1, "2026-03-09", 14.00, "Bookshop")], [other], GBP)[1].status == "waiting"


def test_a_partly_explained_payment_is_left_alone() -> None:
    part = tx(12, "2026-03-10", -14.00, "BOOKSHOP", unexplained=-4.00)
    assert match_receipts([rc(1, "2026-03-09", 14.00, "Bookshop")], [part], GBP)[1].status == "waiting"


def test_the_nearest_identical_payment_is_taken() -> None:
    # receipt on the 6th: the 6th (same day) beats the 5th and the 2nd
    result = match_receipts([rc(1, "2026-03-06", 21.50, "Railco")], FEED, GBP)[1]
    assert result.transaction["url"] == "tx/4" and result.days == 0


def test_a_receipt_without_its_own_date_asks_which_payment() -> None:
    receipt = dict(rc(1, "2026-03-10", 21.50, "Railco"), undated=True)   # photo's date
    result = match_receipts([receipt], FEED, GBP)[1]
    assert result.status == "choose" and [t["url"] for t in result.options] == ["tx/1", "tx/2", "tx/3", "tx/4"]


def test_a_wrongly_chosen_payment_points_to_the_exact_one() -> None:
    """Talk360 £5.49 chosen for a £4.85 Sainsbury's payment five days earlier,
    while its own £5.49 payment sits there: the choice stands, and the
    exact one is offered."""
    feed = [tx(1, "2026-10-02", -4.85, "SAINSBURY'S"), tx(2, "2026-10-08", -5.49, "TALK360")]
    talk = dict(rc(1, "2026-10-07", 5.49, "Talk360 Group B.V."), pinned="tx/1")
    result = match_receipts([talk], feed, GBP)[1]
    assert result.pinned and result.transaction["url"] == "tx/1" and result.better["url"] == "tx/2"
    fair = dict(rc(1, "2026-03-05", 7.50, "Cafe North"), pinned="tx/6")      # a tip: nothing exact
    assert match_receipts([fair], FEED, GBP)[1].better is None
    taken = [talk, rc(2, "2026-10-07", 5.49, "Talk360")]                      # the exact one is another's
    assert match_receipts(taken, feed, GBP)[1].better is None


def test_a_payment_that_cant_take_a_receipt_says_why() -> None:
    """Shown as "No receipt" in Bank Feed, but FreeAgent's explanation is in
    the way: say what, not "already has its receipt"."""
    base = tx(1, "2026-10-08", -5.49, "TALK360")
    assert why_not_open(base) is None
    assert "partly explained (£2.00 of £5.49" in why_not_open({**base, "unexplained_amount": -2.0})
    one = {**base, "unexplained_amount": 0, "explanation_url": "e/1"}
    assert why_not_open(one) is None, "one unlocked explanation, no receipt: attach to it"
    assert "locked" in why_not_open({**one, "explanation_locked": 1})
    assert "already has its receipt" in why_not_open({**one, "explanation_attachments": 1})
    assert "several explanations" in why_not_open({**base, "unexplained_amount": 0})


def test_a_split_payment_keeps_its_receipt_and_a_copy_is_spotted() -> None:
    """Filed before a Reset as two explanations, receipt on the first: the
    payment has its receipt, and the same file added again says so."""
    import tempfile

    from app.db import Database

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        db.save_bank_transactions([{"url": "tx/t", "bank_account": "a", "dated_on": "2026-10-08", "amount": "-5.49",
                                    "unexplained_amount": "0.0", "description": "Talk360 // CARD_PURCHASE",
                                    "bank_transaction_explanations": [{"url": "e/1", "attachment": {"url": "x"}},
                                                                      {"url": "e/2"}]}])
        t = dict(db.bank_transactions(["a"])[0])
    assert t["explanation_attachments"] == 1 and "already has its receipt" in why_not_open(t)
    copy = rc(1, "2026-10-07", 5.49, "Talk360 Group B.V.")
    assert match_receipts([copy], [t], GBP)[1].in_freeagent["url"] == "tx/t"
    other = rc(2, "2026-10-07", 5.49, "Someone Else")
    assert match_receipts([other], [t], GBP)[2].in_freeagent is None, "only when the statement names it"


def test_a_pinned_payment_wins_even_when_the_amount_differs() -> None:
    bill = dict(rc(1, "2026-03-05", 7.50, "Cafe North"), pinned="tx/6")      # £9.00 paid: a tip
    result = match_receipts([bill], FEED, GBP)[1]
    assert result.status == "matched" and result.pinned and not result.exact
    assert result.transaction["url"] == "tx/6"


def test_near_misses_offer_a_slightly_bigger_payment_from_the_same_supplier() -> None:
    result = match_receipts([rc(1, "2026-03-06", 7.50, "Cafe North")], FEED, GBP)[1]
    assert result.status == "waiting"
    assert [(t["url"], t["difference"]) for t in result.options] == [("tx/6", 1.5)]
    # a different supplier's payment is never a near miss
    assert match_receipts([rc(2, "2026-03-06", 7.50, "Bakery")], FEED, GBP)[2].options == []


def test_supplier_token() -> None:
    assert supplier_token("Pret A Manger (Europe) Ltd") == "PRET"
    assert supplier_token("M&S") == ""
    assert supplier_token(None) == ""


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
