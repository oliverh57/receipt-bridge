"""Export and import recurring receipts (the supplier rules in watchers/).

An export is one file, "Recurring receipts.rbrules": the chosen rules'
YAML, exactly as they are, so comments and hand edits survive the trip.
An import takes one of those, or a single rule's .yaml, checks each rule
the way the app checks its own, and adds only the ones you don't already
have: an existing rule is never overwritten.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .watchers import Watcher

SUFFIX = ".rbrules"
FORMAT = 1
MAX_BYTES = 2 * 1024 * 1024


class RulesError(ValueError):
    pass


def bundle(rules: list[tuple[str, str]]) -> bytes:
    """[(file name, YAML text)] as one export file."""
    return json.dumps({
        "receipt_bridge_rules": FORMAT,
        "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rules": [{"file": name, "yaml": text} for name, text in rules],
    }, indent=1).encode("utf-8")


def read(data: bytes, name: str = "") -> list[tuple[str, str]]:
    """The rules in an export file, or in one rule's .yaml: [(file, text)]."""
    if len(data) > MAX_BYTES:
        raise RulesError("That file is too big to be recurring receipts.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RulesError("That isn't a recurring receipts file.") from exc
    try:
        packed = json.loads(text)
    except ValueError:
        packed = None
    if isinstance(packed, dict) and packed.get("receipt_bridge_rules") == FORMAT:
        rules = [(str(r.get("file") or ""), str(r.get("yaml") or "")) for r in packed.get("rules") or []
                 if isinstance(r, dict)]
        if not rules:
            raise RulesError("That file has no recurring receipts in it.")
        return rules
    if name.lower().endswith((".yaml", ".yml")):
        return [(Path(name).name, text)]
    raise RulesError("That isn't a recurring receipts file (.rbrules) or a rule (.yaml).")


def check(text: str) -> Watcher:
    """A rule, loaded the way the app loads its own. Raises RulesError."""
    try:
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict) or not raw.get("id"):
            raise ValueError("it has no id")
        return Watcher.from_dict(raw)
    except Exception as exc:
        raise RulesError(f"isn't a working rule ({str(exc)[:120]})") from exc


def _file_name(name: str, rule_id: str) -> str:
    stem = re.sub(r"[^a-z0-9_-]+", "-", (Path(name).stem or rule_id).lower()).strip("-")
    if not stem or stem.startswith("_"):
        stem = re.sub(r"[^a-z0-9_-]+", "-", rule_id.lower()).strip("-") or "rule"
    return f"{stem}.yaml"


def install(rules: list[tuple[str, str]], directory: Path, have: set[str]) -> dict[str, Any]:
    """Add the rules not already here (by id). `have`: the ids already
    here. Returns {added: [{id, name}], skipped: [{name, why}]}."""
    added, skipped = [], []
    for name, text in rules:
        try:
            rule = check(text)
        except RulesError as exc:
            skipped.append({"name": name or "A rule", "why": str(exc)})
            continue
        if rule.id in have:
            skipped.append({"name": rule.name, "why": "you already have it"})
            continue
        target = directory / _file_name(name, rule.id)
        n = 2
        while target.exists():                       # a different rule with that file name
            target = directory / f"{target.stem.rsplit('-', 1)[0]}-{n}.yaml"
            n += 1
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / f".{target.name}.part"     # hidden: never loaded half-written
        temp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        temp.replace(target)
        have.add(rule.id)
        added.append({"id": rule.id, "name": rule.name})
    return {"added": added, "skipped": skipped}
