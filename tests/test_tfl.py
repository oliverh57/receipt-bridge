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
    page = tfl.receipt_html({"dated_on": "2026-10-05", "amount": -9.9, "description": "TFL // CARD_PURCHASE///"},
                            "Mettle Business")
    assert "5 October 2026" in page and "£9.90" in page and "Mettle Business" in page
    assert "£0.00 (fares are zero-rated)" in page
    assert "not a document from TfL" in page
    assert "CARD_PURCHASE" not in page                    # the statement line as the bank shows it


# ---- through the app --------------------------------------------------------------


def make(*payments: tuple[str, float, str]) -> tuple[ReceiptService, list[dict], tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    service = ReceiptService(Config(raw={
        "data_dir": str(base / "data"), "export_dir": str(base / "export"),
        "photo_inbox": str(base / "inbox"), "freeagent": {"credentials_file": str(base / "none.json")}}))
    service.db.set_state("freeagent:reference", json.dumps(
        {"bank_accounts": [{"url": ACCOUNT, "currency": "GBP", "name": "Business"}],
         "categories": [{"url": TRAVEL, "description": "Travel", "nominal_code": "365", "group": "admin"}]}))
    service.db.set_state("freeagent:accounts", json.dumps([ACCOUNT]))
    paid = (datetime.now() - timedelta(days=3)).date().isoformat()
    service.db.save_bank_transactions([
        {"url": url, "bank_account": ACCOUNT, "dated_on": paid, "amount": amount, "unexplained_amount": amount,
         "description": description, "explanation_url": f"{url}/explanation"}
        for url, amount, description in payments])
    printed: list[dict] = []

    def render(pool, payment, out_path, account_name="", pdf_config=None):   # no Chromium in tests
        printed.append({"payment": payment, "account": account_name})
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
        assert printed[0]["account"] == "Business"
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


def test_automatically_only_tfl_payments_and_only_when_switched_on() -> None:
    service, printed, tmp = make(("tx/a", -2.3, "TFL // CARD_PURCHASE///"), ("tx/b", -7.2, "TFL // CARD_PURCHASE///"),
                                 ("tx/shop", -4.0, "TESCO STORES"))
    with tmp:
        assert service.snapshot()["tfl_receipts"] is False
        service.db.set_state("pref:tfl_receipts", "1")
        assert service.snapshot()["tfl_receipts"] is True
        service._run_make_tfl_receipts(None)
        assert sorted(p["payment"]["url"] for p in printed) == ["tx/a", "tx/b"]
        assert "Made 2 TfL receipts" in service.snapshot()["outcome"]["message"]


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
