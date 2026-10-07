"""Category suggestions ("AI guess"): only ever one of your FreeAgent
categories, shown as a guess, never filed automatically. Made-up values.

The on-device model is replaced by a fake runner here; the last test runs
the real helper when this Mac has the model, and is skipped otherwise.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import category_guess  # noqa: E402
from app.config import Config  # noqa: E402
from app.service import ReceiptService  # noqa: E402

CATS = [
    {"url": "c/travel", "description": "Travel", "group": "admin_expenses_categories"},
    {"url": "c/meals", "description": "Accommodation and Meals", "group": "admin_expenses_categories"},
    {"url": "c/software", "description": "Computer Software", "group": "admin_expenses_categories"},
    {"url": "c/drawings", "description": "Drawings", "group": "general_categories"},
]
REFERENCE = {"categories": CATS}


class FakeRun:
    """Stands in for the Swift helper: answers from a dict."""

    def __init__(self, answers: dict[str, str], model: str = "available"):
        self.answers, self.model, self.requests = answers, model, []

    def __call__(self, args, input, **_kw):
        request = json.loads(input)
        self.requests.append(request)
        guesses = {str(r["id"]): self.answers[r["supplier"]] for r in request["receipts"] if r["supplier"] in self.answers}
        return subprocess.CompletedProcess(args, 0, json.dumps({"model": self.model, "guesses": guesses}), "")


def test_only_expense_categories_are_offered() -> None:
    assert sorted(category_guess.expense_categories(REFERENCE)) == ["Accommodation and Meals", "Computer Software", "Travel"]


def test_a_made_up_category_from_the_model_is_dropped() -> None:
    run = FakeRun({"Railco": "Travel", "Odd Ltd": "Spaceships"})
    found = category_guess.guess([{"id": 1, "supplier": "Railco", "text": ""},
                                  {"id": 2, "supplier": "Odd Ltd", "text": ""}], REFERENCE, Path("."), run)
    assert found == {1: {"url": "c/travel", "name": "Travel", "source": "model"}}
    assert "Drawings" not in run.requests[0]["categories"]


def test_without_the_model_generic_words_still_suggest() -> None:
    run = FakeRun({}, model="unavailable: Apple Intelligence is off")
    found = category_guess.guess([{"id": 1, "supplier": "Example Cabs", "text": "Taxi ride, 3.2 miles"},
                                  {"id": 2, "supplier": "Example Shop", "text": "Widgets"}], REFERENCE, Path("."), run)
    assert found == {1: {"url": "c/travel", "name": "Travel", "source": "words"}}


def test_a_word_only_suggests_a_category_you_have() -> None:
    assert category_guess.word_guess("Example Cafe", "Coffee", ["Travel"]) is None
    assert category_guess.word_guess("Example Cafe", "Coffee", ["Accommodation and Meals"]) == "Accommodation and Meals"


def make() -> tuple[ReceiptService, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    service = ReceiptService(Config(raw={
        "data_dir": str(base / "data"), "export_dir": str(base / "export"),
        "photo_inbox": str(base / "inbox"), "freeagent": {"credentials_file": str(base / "none.json")}}))
    service.db.set_state("freeagent:reference", json.dumps(REFERENCE))
    return service, tmp


def test_a_guess_is_shown_as_a_guess_and_asked_only_once() -> None:
    service, tmp = make()
    with tmp:
        rid = service.db.insert_receipt({"watcher_id": "railco", "gmail_message_id": f"m{time.time_ns()}",
                                         "vendor": "Railco", "purchased_on": "2026-03-04", "total": 21.5,
                                         "currency": "GBP", "subject": "Your train tickets"})
        run = FakeRun({"Railco": "Travel"})
        original = category_guess.model_guesses
        category_guess.model_guesses = lambda items, names, data_dir, runner=None: original(items, names, data_dir, run)
        try:
            assert service.guess_categories()
            service._run_guess_categories()
            service._run_guess_categories()
        finally:
            category_guess.model_guesses = original
            service.stop()
        assert len(run.requests) == 1, "asked again"
        assert "train tickets" in run.requests[0]["receipts"][0]["text"]
        item = next(r for r in service.receipts("pending") if r["id"] == rid)
        assert item["category"] == "c/travel" and item["category_name"] == "Travel"
        assert item["ai_guess"]["category"] is True
        assert "Choose a category" not in item["reasons"]

        service.set_receipt_fields(rid, {"category": "c/software"})        # your choice wins
        item = next(r for r in service.receipts("pending") if r["id"] == rid)
        assert item["category_name"] == "Computer Software" and item["ai_guess"]["category"] is False


def test_the_real_helper_when_this_mac_has_the_model() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        if category_guess.ensure_built(Path(tmp)) is None:
            print("    (skipped: helper can't be built here)")
            return
        guesses, status = category_guess.model_guesses(
            [{"id": 1, "supplier": "Example Rail", "text": "Off-peak return, 2 adults, platform 4"}],
            ["Travel", "Accommodation and Meals", "Computer Software"], Path(tmp))
        if status != "available":
            print(f"    (skipped: {status})")
            return
        assert guesses.get(1) in ("Travel", "Accommodation and Meals", "Computer Software")


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
