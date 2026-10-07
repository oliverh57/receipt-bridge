"""Edit an existing supplier rule without damaging it.

Rules are YAML files, some hand-written with comments explaining each
decision (why Trainline needs a browser, why Yesim must mention its own
name). A plain load-and-dump would silently delete every one of those
comments. This edits through a round-trip parser instead, so only the values
that changed are touched and everything else in the file survives as written.

Only the settings that make sense for any supplier are editable here: its
name, whether it's on, and which emails it matches. How a rule reads totals
and references stays in the file, where it can be as specific as it needs.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .watchers import Watcher

EDITABLE = ("name", "enabled", "domain", "subject", "mentions", "paid_with")


def _yaml() -> YAML:
    y = YAML()  # round-trip: comments, ordering and quoting are kept
    y.preserve_quotes = True
    y.width = 100
    # Match the rule files' own style (lists indented under their key), so
    # an edit changes only the edited lines rather than reformatting the file.
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _load(path: Path) -> Any:
    return _yaml().load(path.read_text(encoding="utf-8")) or {}


def build_query(domain: str, subject: str, mentions: str) -> str:
    """The Gmail search for a rule. Shared with the supplier builder."""
    mentions = mentions.replace('"', "").strip()
    return (
        f"from:({domain})"
        + (f' subject:("{subject}")' if subject else "")
        + (f' "{mentions}"' if mentions else "")
    )


def read(path: Path) -> dict[str, Any]:
    """The editable settings of a rule, as plain values."""
    doc = _load(path)
    match = doc.get("match") or {}
    return {
        "id": str(doc.get("id", path.stem)),
        "name": str(doc.get("name") or doc.get("id") or path.stem),
        "enabled": bool(doc.get("enabled", True)),
        "domain": str(match.get("from_contains") or ""),
        "subject": str(match.get("subject_contains") or ""),
        "mentions": str(match.get("body_contains") or ""),
        "paid_with": str(doc.get("paid_with") or "business"),
    }


def apply(path: Path, changes: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The rule's new text and plain form, with `changes` applied.

    Nothing is written. Fields not in `changes` are left exactly as they are,
    and the Gmail query is rebuilt only if a matching setting changed — so
    renaming a hand-written rule never rewrites its hand-tuned query.
    """
    doc = _load(path)
    before = read(path)
    after = {**before, **{k: v for k, v in changes.items() if k in EDITABLE}}

    name = str(after["name"]).strip()
    if not name:
        raise ValueError("A supplier needs a name")
    domain = str(after["domain"]).strip().lower()
    if not domain:
        raise ValueError("Say who the emails come from")

    if name != before["name"]:
        doc["name"] = name
        doc["vendor"] = name
    if bool(after["enabled"]) != before["enabled"]:
        doc["enabled"] = bool(after["enabled"])
    paid_with = str(after.get("paid_with") or "business").strip()
    if paid_with not in ("business", "personal") and not paid_with.startswith("https://"):
        raise ValueError("Paid with is a bank account, business or personal")
    if paid_with != before["paid_with"]:
        if paid_with == "business":
            doc.pop("paid_with", None)
        else:
            doc["paid_with"] = paid_with

    match_changed = any(
        str(after[k]).strip() != str(before[k]).strip() for k in ("domain", "subject", "mentions")
    )
    if match_changed:
        match = doc.get("match")
        if match is None:
            doc["match"] = match = {}
        for key, field in (("domain", "from_contains"), ("subject", "subject_contains"), ("mentions", "body_contains")):
            value = str(after[key]).strip()
            if key == "domain":
                value = domain
            if value:
                match[field] = value
            elif field in match:
                del match[field]
        doc["gmail_query"] = build_query(domain, str(after["subject"]).strip(), str(after["mentions"]).strip())

    buffer = io.StringIO()
    _yaml().dump(doc, buffer)
    text = buffer.getvalue()

    # Refuse to produce anything the app itself couldn't load.
    plain = YAML(typ="safe").load(text)
    Watcher.from_dict(plain, path)
    return text, plain


def save(path: Path, changes: dict[str, Any]) -> dict[str, Any]:
    text, plain = apply(path, changes)
    # Write beside the original, then swap: a crash mid-write must never
    # leave a half-written rule that stops the supplier loading at all.
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".yaml")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(temp, path)
    except Exception:
        Path(temp).unlink(missing_ok=True)
        raise
    return plain


def delete(path: Path, bin_dir: Path) -> str:
    """Move a rule out of use. Returns the name it can be restored by.

    Moved, not erased: the built-in rules hold hard-won detail (Trainline's
    browser steps, Yesim's merchant check) that would be tedious to rebuild
    after a mis-click. Receipts already collected are untouched either way.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = bin_dir / f"{path.stem}--{stamp}{path.suffix}"
    shutil.move(str(path), str(target))
    return target.name


def restore(name: str, bin_dir: Path, watchers_dir: Path) -> Path:
    """Put a deleted rule back. Never overwrites one added since."""
    source = (bin_dir / name).resolve()
    if source.parent != bin_dir.resolve() or not source.is_file() or name.startswith("."):
        raise ValueError("Nothing to restore")
    stem = name.split("--")[0]
    target = watchers_dir / f"{stem}{source.suffix}"
    n = 2
    while target.exists():
        target = watchers_dir / f"{stem}-{n}{source.suffix}"
        n += 1
    shutil.move(str(source), str(target))
    return target
