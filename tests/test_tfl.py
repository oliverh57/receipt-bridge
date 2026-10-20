"""TfL receipts: made from the bank payment, since TfL sends none for
contactless travel. Made-up payments; the PDF printing is stubbed.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import tfl  # noqa: E402
from app.config import Config  # noqa: E402
from app.service import ReceiptService  # noqa: E402

ACCOUNT = "https://fa.test/v2/bank_accounts/1"
TRAVEL = "https://fa.test/v2/categories/365"


# ---- the pure part ------------------------------------------------------------


def test_tfl_payments_are_known_by_their_statement_line() -> None:
    for line in ("TFL // CARD_PURCHASE///", "TFL TRAVEL CH TFL.GOV.UK/CP", "Transport for London", "tfl.gov.uk/cp"):
        assert tfl.is_tfl(line), line
    for line in ("NETFLIX.COM // CARD_PURCHASE", "TFLX LTD", "AMAZON* MKTPLACE", "", None):
        assert not tfl.is_tfl(line), line


def test_the_page_shows_the_payment_and_says_where_it_came_from() -> None:
    payment = {"url": "https://api.freeagent.com/v2/bank_transactions/98765", "dated_on": "2026-10-05",
               "amount": -9.9, "description": "TFL // CARD_PURCHASE///"}
    page = tfl.receipt_html(payment, "Mettle Business", "Example Ltd")
    assert "5 October 2026" in page and "£9.90" in page and "Mettle Business" in page and "Example Ltd" in page
    assert "TFL-20261005-98765" in page and tfl.receipt_number(payment) == "TFL-20261005-98765"
    assert "0% (zero-rated)" in page and "£0.00" in page
    assert "It is not issued by Transport for London" in page
    assert "CARD_PURCHASE" not in page                    # the statement line as the bank shows it


def test_without_a_company_the_page_still_reads() -> None:
    page = tfl.receipt_html({"dated_on": "2026-10-05", "amount": -9.9, "description": "TFL"})
    assert "Travel record" in page and "TFL-20261005-0" in page and "of the account named above" not in page
    assert 'aria-label="Receipt Bridge"' in page               # the logo
    assert "journeys not recorded" in page and "entered by the traveller" not in page


def test_the_journey_you_entered_is_on_the_record() -> None:
    trip = {"from": "King's Cross St. Pancras", "to": "Canary Wharf", "date": "2026-10-04", "reason": "Client meeting"}
    page = tfl.receipt_html({"dated_on": "2026-10-05", "amount": -9.9, "description": "TFL"}, trip=trip)
    assert "King&#x27;s Cross St. Pancras" in page and "Canary Wharf" in page
    assert "4 October 2026" in page and "Client meeting" in page
    assert "entered by the traveller and are not confirmed by Transport for London" in page
    assert "<dd>Single</dd>" in page and "Back to" not in page
    assert "Sunday 4 October 2026" in page                    # the day travelled heads the record
    there_and_back = tfl.receipt_html({"dated_on": "2026-10-05", "amount": -9.9}, trip={**trip, "return": True})
    assert "Back to King&#x27;s Cross St. Pancras" in there_and_back and "<dd>Return</dd>" in there_and_back
    only_start = tfl.receipt_html({"dated_on": "2026-10-05", "amount": -9.9}, trip={"from": "Bank"})
    assert "Bank" in only_start and "Not recorded" in only_start


def test_journeys_are_tidied() -> None:
    assert tfl.journey({"from": "  Oxford   Circus ", "to": "", "date": "4 Oct", "reason": "x" * 300}) == \
        {"from": "Oxford Circus", "reason": "x" * 200}
    assert tfl.journey({"date": "2026-10-04"}) == {"date": "2026-10-04"}
    assert tfl.journey(None) == {}
    assert tfl.describe({"from": "Bank", "to": "Angel"}) == "TfL travel: Bank to Angel"
    assert tfl.describe({"to": "Angel"}) == "TfL travel: to Angel"
    assert tfl.describe({"from": "Bank", "to": "Angel", "return": True}) == "TfL travel: Bank to Angel and back"
    assert tfl.journey({"return": True}) == {"return": True}
    assert tfl.journey({"return": "yes", "from": "Bank"}) == {"from": "Bank"}      # only a real true
    assert tfl.describe({}) == "Contactless travel"


def test_stations_are_offered_for_start_and_end() -> None:
    names = tfl.stations()
    assert len(names) > 300 and not any(n.startswith("#") for n in names)
    for name in ("Oxford Circus", "Canary Wharf", "Stratford", "Clapham Junction", "Abbey Wood", "Wimbledon"):
        assert name in names, name
    assert not any(n.endswith(("Underground Station", "DLR Station", "Rail Station", " LL")) for n in names)
    # National Rail too (contactless works there), after TfL's own: ranked behind on an equal match
    assert "London St Pancras International" in names and "Gatwick Airport" in names
    assert names.index("Abbey Wood") < names.index("King's Cross St. Pancras") < names.index("London St Pancras International")


# ---- through the app --------------------------------------------------------------


def make(*payments: tuple[str, float, str]) -> tuple[ReceiptService, list[dict], tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    service = ReceiptService(Config(raw={
        "data_dir": str(base / "data"), "export_dir": str(base / "export"),
        "photo_inbox": str(base / "inbox"), "freeagent": {"credentials_file": str(base / "none.json")}}))
    service.db.set_state("freeagent:reference", json.dumps(
        {"bank_accounts": [{"url": ACCOUNT, "currency": "GBP", "name": "Business"}],
         "vat": {"registered": True, "company": "Example Ltd"},
         "categories": [{"url": TRAVEL, "description": "Travel", "nominal_code": "365", "group": "admin"}]}))
    service.db.set_state("freeagent:accounts", json.dumps([ACCOUNT]))
    paid = (datetime.now() - timedelta(days=3)).date().isoformat()
    service.db.save_bank_transactions([
        {"url": url, "bank_account": ACCOUNT, "dated_on": paid, "amount": amount, "unexplained_amount": amount,
         "description": description, "explanation_url": f"{url}/explanation"}
        for url, amount, description in payments])
    printed: list[dict] = []

    def render(pool, payment, out_path, account_name="", pdf_config=None, company="", journey=None):  # no Chromium
        printed.append({"payment": payment, "account": account_name, "company": company, "journey": journey})
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"%PDF-1.4 tfl")
        return out_path

    tfl.render = render                                    # type: ignore[assignment]
    return service, printed, tmp


def row(service: ReceiptService, url: str) -> dict:
    sheet = service.statement(None, (datetime.now() - timedelta(days=3)).strftime("%Y-%m"))
    return next(r for r in sheet["rows"] if r["url"] == url)


def test_make_tfl_receipt_puts_one_in_match_paired_with_the_payment() -> None:
    service, printed, tmp = make(("tx/tfl", -9.9, "TFL // CARD_PURCHASE///"), ("tx/shop", -4.0, "TESCO STORES"))
    with tmp:
        assert row(service, "tx/tfl")["tfl"] is True
        assert row(service, "tx/shop")["tfl"] is False

        service._run_make_tfl_receipts(["tx/tfl"])
        assert printed[0]["account"] == "Business" and printed[0]["company"] == "Example Ltd"
        receipt = next(r for r in service.receipts("pending") if r["supplier"] == "Transport for London")
        assert receipt["total"] == 9.9 and receipt["pinned_payment"] == "tx/tfl"
        assert receipt["document"] == "generated"
        saved = service.db.get_receipt(receipt["id"])
        assert saved["vat"] == 0.0 and saved["category"] == TRAVEL and saved["paid_by"] == "business"
        assert json.loads(saved["extra_json"])["vat_choice"] == "0.0"
        assert row(service, "tx/tfl")["status"] == "in_match"
        assert service.snapshot()["outcome"]["ok"] is True

        # asked again: nothing new
        service._run_make_tfl_receipts(["tx/tfl"])
        assert len(printed) == 1
        assert service.snapshot()["outcome"]["ok"] is False


def test_an_ignored_tfl_receipt_is_not_made_again_by_itself() -> None:
    service, printed, tmp = make(("tx/tfl", -9.9, "TFL // CARD_PURCHASE///"))
    with tmp:
        service._run_make_tfl_receipts(None)
        rid = next(r["id"] for r in service.receipts("pending"))
        service.db.set_status(rid, "ignored")
        service._run_make_tfl_receipts(None)
        assert len(printed) == 1


def test_asking_again_replaces_one_you_removed_or_deleted() -> None:
    service, printed, tmp = make(("tx/tfl", -9.9, "TFL // CARD_PURCHASE///"))
    with tmp:
        service._run_make_tfl_receipts(["tx/tfl"])
        first = next(r["id"] for r in service.receipts("pending"))
        service.remove_file_from_payment(first, "tx/tfl")      # "Remove file"
        assert row(service, "tx/tfl")["status"] == "missing"

        service._run_make_tfl_receipts(None)                  # by itself: you removed it, so no
        assert len(printed) == 1

        service._run_make_tfl_receipts(["tx/tfl"])            # asked: a new one, the old one gone
        assert len(printed) == 2 and service.snapshot()["outcome"]["ok"] is True
        assert service.db.get_receipt(first)["status"] == "deleted"
        second = next(r["id"] for r in service.receipts("pending"))
        assert second != first and row(service, "tx/tfl")["status"] == "in_match"

        service._delete_receipt(second)                       # deleted: asked again, made again
        service._run_make_tfl_receipts(["tx/tfl"])
        assert len(printed) == 3 and row(service, "tx/tfl")["status"] == "in_match"


def test_automatically_only_tfl_payments_and_only_when_switched_on() -> None:
    service, printed, tmp = make(("tx/a", -2.3, "TFL // CARD_PURCHASE///"), ("tx/b", -7.2, "TFL // CARD_PURCHASE///"),
                                 ("tx/shop", -4.0, "TESCO STORES"))
    with tmp:
        assert service.snapshot()["tfl_receipts"] is False
        service.db.set_state("pref:tfl_receipts", "1")
        assert service.snapshot()["tfl_receipts"] is True
        service._run_make_tfl_receipts(None)
        assert sorted(p["payment"]["url"] for p in printed) == ["tx/a", "tx/b"]
        assert "Generated 2 travel records" in service.snapshot()["outcome"]["message"]


def test_the_journey_typed_is_used_else_the_usual_one() -> None:
    service, printed, tmp = make(("tx/a", -2.3, "TFL // CARD_PURCHASE///"), ("tx/b", -7.2, "TFL // CARD_PURCHASE///"))
    with tmp:
        service.set_tfl_journey("  Bank ", "Angel", "Office")
        assert service.snapshot()["tfl_journey"] == {"from": "Bank", "to": "Angel", "reason": "Office", "return": False}

        service._run_make_tfl_receipts(["tx/a"], {"from": "Oxford Circus", "to": "", "date": "2026-10-04",
                                                  "reason": "Client meeting"})
        assert printed[-1]["journey"] == {"from": "Oxford Circus", "date": "2026-10-04", "reason": "Client meeting"}
        saved = service.db.get_receipt(next(r["id"] for r in service.receipts("pending")))
        assert saved["description"] == "TfL travel: from Oxford Circus"
        assert json.loads(saved["extra_json"])["journey"]["reason"] == "Client meeting"

        service._run_make_tfl_receipts(None)                  # by itself: the usual journey
        assert printed[-1]["payment"]["url"] == "tx/b"
        assert printed[-1]["journey"] == {"from": "Bank", "to": "Angel", "reason": "Office"}

        service.set_tfl_journey("", None, "", True)           # cleared; "to" left as it was
        assert service.tfl_journey == {"from": "", "to": "Angel", "reason": "", "return": True}
        service.db.set_status(next(r["id"] for r in service.receipts("pending") if r["pinned_payment"] == "tx/b"),
                              "ignored")
        service._run_make_tfl_receipts(["tx/b"])               # asked again, no journey given: the usual one
        assert printed[-1]["journey"] == {"to": "Angel", "return": True}


def test_the_api_takes_the_journey_and_lists_stations() -> None:
    from fastapi.testclient import TestClient

    from app.api import create_app

    service, printed, tmp = make(("tx/tfl", -9.9, "TFL // CARD_PURCHASE///"))
    with tmp:
        asked = []
        service.make_tfl_receipt = lambda url, journey=None, link=False: asked.append((url, journey)) or True  # type: ignore
        app = create_app(service)
        client = TestClient(app, base_url="http://127.0.0.1")
        headers = {"x-receipt-bridge": app.state.token}
        client.post("/api/statement/tfl-receipt", json={"url": "tx/tfl"}, headers=headers)
        client.post("/api/statement/tfl-receipt", json={"url": "tx/tfl", "from": "Bank", "reason": "Meeting"},
                    headers=headers)
        client.post("/api/statement/tfl-receipt", json={"url": "tx/tfl", "return": True}, headers=headers)
        assert asked == [("tx/tfl", None),
                         ("tx/tfl", {"from": "Bank", "to": "", "date": "", "reason": "Meeting", "return": False}),
                         ("tx/tfl", {"from": "", "to": "", "date": "", "reason": "", "return": True})]
        for bad in ({"from": 3}, {"return": "yes"}):
            assert client.post("/api/statement/tfl-receipt", json={"url": "tx/tfl", **bad},
                               headers=headers).status_code == 400
        client.post("/api/settings", json={"tfl_from": "Bank", "tfl_reason": "Office", "tfl_return": True},
                    headers=headers)
        assert service.tfl_journey == {"from": "Bank", "to": "", "reason": "Office", "return": True}
        stations = client.get(f"/api/tfl/stations?t={app.state.token}").json()
        assert "Oxford Circus" in stations


def test_the_dialog_previews_the_record_without_keeping_anything() -> None:
    from fastapi.testclient import TestClient

    from app.api import create_app

    service, printed, tmp = make(("tx/tfl", -9.9, "TFL // CARD_PURCHASE///"))
    with tmp:
        app = create_app(service)
        client = TestClient(app, base_url="http://127.0.0.1")
        r = client.get("/api/statement/tfl-preview", params={"url": "tx/tfl", "start": "Bank", "end": "Angel",
                                                             "back": "true", "t": app.state.token})
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
        assert printed[-1]["journey"] == {"from": "Bank", "to": "Angel", "return": True}
        assert printed[-1]["account"] == "Business" and printed[-1]["company"] == "Example Ltd"
        assert service.receipts("pending") == [], "a preview makes no receipt"
        assert not any((service.config.pdf_dir / "previews").glob("*")), "nor leaves a file"
        assert client.get("/api/statement/tfl-preview", params={"url": "tx/gone", "t": app.state.token}).status_code == 404


def test_made_from_the_dialog_it_is_linked_too() -> None:
    service, printed, tmp = make(("tx/a", -9.9, "TFL // CARD_PURCHASE///"), ("tx/b", -4.6, "TFL // CARD_PURCHASE///"))
    with tmp:
        filed: list[list[int]] = []
        service._run_file = lambda ids, only_single_part=False: filed.append(ids)  # type: ignore[method-assign]
        service._freeagent_connected = lambda: False                             # type: ignore[method-assign]
        service._run_make_tfl_receipts(["tx/a"], {"from": "Bank"}, link=True)
        assert filed == [] and "isn't connected" in service.snapshot()["outcome"]["message"]
        assert row(service, "tx/a")["status"] == "in_match", "left in Match to link later"

        service._freeagent_connected = lambda: True                              # type: ignore[method-assign]
        service._run_make_tfl_receipts(["tx/b"], {"from": "Bank"}, link=True)
        made = next(r["id"] for r in service.receipts("pending") if r["pinned_payment"] == "tx/b")
        assert filed == [[made]], "linked as Approve does"

        service._run_make_tfl_receipts(None)                                  # by itself: never linked
        assert len(filed) == 1


def test_a_category_you_chose_for_tfl_before_is_kept() -> None:
    service, printed, tmp = make(("tx/tfl", -9.9, "TFL // CARD_PURCHASE///"))
    with tmp:
        service.db.set_state("category_for:transport for london", "https://fa.test/v2/categories/999")
        service._run_make_tfl_receipts(["tx/tfl"])
        saved = service.db.get_receipt(next(r["id"] for r in service.receipts("pending")))
        assert saved["category"] is None                  # the remembered one applies
        assert service._category_for(saved) == "https://fa.test/v2/categories/999"


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
