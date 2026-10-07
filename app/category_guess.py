"""Suggest a FreeAgent category for a receipt from a new supplier ("AI
guess", PLAN.md §11). On this Mac only.

First the on-device model (`native/category-guess.swift`), which can only
answer with one of your FreeAgent category names. Without it (an older Mac,
Apple Intelligence off), a few generic words: "taxi" suggests Travel, if you
have a category called Travel. Either way it's a suggestion: the receipt
still waits for you in Needs you, and nothing files on a guess by itself.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from .config import ROOT

log = logging.getLogger(__name__)

SOURCE = ROOT / "native" / "category-guess.swift"
TIMEOUT_SECONDS = 300
# Categories a receipt can go to: running costs and cost of sales, not
# balance-sheet entries (drawings, loans, capital assets).
EXPENSE_GROUPS = ("admin_expenses_categories", "cost_of_sales_categories")
_build_lock = threading.Lock()

# Generic words → FreeAgent's standard category names. Used only when the
# model isn't available, and only when the user has a category of that name.
WORDS: tuple[tuple[str, str], ...] = (
    (r"\b(train|trains|rail|railway|railcard|taxi|cab|ride|trip|bus|coach|tram|metro|underground|"
     r"airline|airways|flight|boarding pass|ferry|parking|toll|fare)\b", "Travel"),
    (r"\b(hotel|hostel|guesthouse|inn|lodge|restaurant|cafe|café|coffee|bakery|pizzeria|bistro|bar|pub|"
     r"diner|kitchen|sandwich|lunch|dinner|breakfast|meal)\b", "Accommodation and Meals"),
    (r"\b(software|subscription|saas|licen[cs]e|cloud|hosting|domain|api|plan renewal)\b", "Computer Software"),
    (r"\b(broadband|mobile|phone|sim|esim|data plan|roaming|internet)\b", "Telephone and Internet"),
    (r"\b(stationery|printer|ink|toner|paper|envelopes|stamps|postage)\b", "Office Costs"),
    (r"\b(accountant|accountancy|bookkeeping|accounting software|legal|solicitor)\b", "Accountancy Fees"),
)


def helper_path(data_dir: Path) -> Path:
    return data_dir / "bin" / "category-guess"


def ensure_built(data_dir: Path) -> Path | None:
    """Compile the helper if it's missing or older than its source; None if
    it can't be built (no Xcode tools): the word fallback still works."""
    binary = helper_path(data_dir)
    with _build_lock:
        if binary.exists() and binary.stat().st_mtime >= SOURCE.stat().st_mtime:
            return binary
        binary.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(["swiftc", "-O", str(SOURCE), "-o", str(binary)],
                                    capture_output=True, text=True, timeout=600)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            log.info("category-guess not built: %s", exc)
            return None
        if result.returncode != 0:
            log.warning("category-guess did not compile: %s", result.stderr.strip()[:300])
            return None
        return binary


def expense_categories(reference: dict[str, Any]) -> dict[str, str]:
    """Category name → URL, for the categories a receipt can go to."""
    return {c["description"]: c["url"] for c in reference.get("categories", [])
            if c.get("group") in EXPENSE_GROUPS and c.get("description")}


def word_guess(supplier: str, text: str, names: list[str]) -> str | None:
    """The fallback: a generic word in the supplier name or receipt."""
    haystack = f"{supplier}\n{text}".lower()
    available = {n.lower(): n for n in names}
    for pattern, category in WORDS:
        if category.lower() in available and re.search(pattern, haystack):
            return available[category.lower()]
    return None


def model_guesses(items: list[dict[str, Any]], names: list[str], data_dir: Path,
                  runner: Any = subprocess.run) -> tuple[dict[int, str], str]:
    """Ask the on-device model. Returns ({receipt id: category name}, status)."""
    binary = ensure_built(data_dir) if runner is subprocess.run else Path("category-guess")
    if binary is None:
        return {}, "unavailable: helper not built"
    request = json.dumps({"categories": names, "receipts": [
        {"id": int(i["id"]), "supplier": i.get("supplier") or "", "text": i.get("text") or ""} for i in items]})
    try:
        result = runner([str(binary)], input=request, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {}, f"failed: {exc}"
    if result.returncode != 0:
        return {}, f"failed: {result.stderr.strip()[:200]}"
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        return {}, "failed: unreadable answer"
    allowed = set(names)
    guesses = {int(k): v for k, v in (answer.get("guesses") or {}).items() if v in allowed}
    return guesses, str(answer.get("model", ""))


def guess(items: list[dict[str, Any]], reference: dict[str, Any], data_dir: Path,
          runner: Any = subprocess.run) -> dict[int, dict[str, str]]:
    """{receipt id: {"url", "name", "source": "model"|"words"}} for the items
    a category could be suggested for. Items: {id, supplier, text}."""
    categories = expense_categories(reference)
    if not items or not categories:
        return {}
    names = sorted(categories)
    found, status = model_guesses(items, names, data_dir, runner)
    if status != "available":
        log.info("category guesses without the model (%s)", status)
    out: dict[int, dict[str, str]] = {}
    for item in items:
        rid = int(item["id"])
        name, source = found.get(rid), "model"
        if not name:
            name, source = word_guess(item.get("supplier") or "", item.get("text") or "", names), "words"
        if name:
            out[rid] = {"url": categories[name], "name": name, "source": source}
    return out
