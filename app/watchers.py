"""The watcher engine.

A watcher is a YAML file describing one kind of receipt email: how to find it
in Gmail, how to confirm a candidate really is that email, which values to pull
out of it, where to get a PDF, and what to call the file.

Nothing here is Trainline-specific. Adding a new vendor means dropping another
YAML file into `watchers/` — no Python required, unless the vendor needs a
browser-automation fetcher, which is a separate opt-in plugin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from dateutil import parser as date_parser

from .email_message import Email

# Standard fields every receipt has. Anything else a watcher extracts is kept
# in `extra` and stays available to the filename template.
CORE_FIELDS = ("reference", "total", "currency", "purchased_on", "description")

DEFAULT_FILENAME = "{purchased_on} {vendor} {currency}{total} {reference}.pdf"

# Characters that have no business being in a filename on any platform.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MONEY = re.compile(r"[^0-9.\-]")


class WatcherError(Exception):
    """Raised when a watcher definition is malformed or extraction fails."""


@dataclass
class FieldSpec:
    name: str
    patterns: list[str] = field(default_factory=list)
    value: Any = None
    source: str = "text"
    type: str = "string"
    date_formats: list[str] = field(default_factory=list)
    template: str | None = None
    join: str = " "
    required: bool = False
    default: Any = None

    @classmethod
    def parse(cls, name: str, raw: Any) -> "FieldSpec":
        # `field: "a regex"` and `field: ["regex", "regex"]` are shorthands.
        if isinstance(raw, str):
            raw = {"patterns": [raw]}
        elif isinstance(raw, list):
            raw = {"patterns": raw}
        if not isinstance(raw, dict):
            raise WatcherError(f"field '{name}' must be a string, list or mapping")

        patterns = raw.get("patterns", raw.get("pattern", []))
        if isinstance(patterns, str):
            patterns = [patterns]

        formats = raw.get("date_formats", [])
        if isinstance(formats, str):
            formats = [formats]

        return cls(
            name=name,
            patterns=list(patterns),
            value=raw.get("value"),
            source=raw.get("source", "text"),
            type=raw.get("type", "string"),
            date_formats=list(formats),
            template=raw.get("template"),
            join=raw.get("join", " "),
            required=bool(raw.get("required", False)),
            default=raw.get("default"),
        )


@dataclass
class Watcher:
    id: str
    name: str
    vendor: str
    gmail_query: str
    enabled: bool = True
    match: dict[str, Any] = field(default_factory=dict)
    fields: list[FieldSpec] = field(default_factory=list)
    pdf: list[Any] = field(default_factory=lambda: ["render_email"])
    filename: str = DEFAULT_FILENAME
    # How far back the first scan reaches, when this supplier needs a
    # different window from the global default. A supplier you buy from twice
    # a year needs a wider net than one you use weekly — and widening the
    # global setting to suit the rare one would drag in a year of the common
    # one, which for Trainline means hundreds of browser round-trips.
    lookback_days: int | None = None
    # Who pays this supplier: "business" (any chosen bank account, the
    # default), "personal" (an expense claim), or one bank account's URL.
    paid_with: str = "business"
    path: Path | None = None
    source_yaml: str = ""

    # ---- loading --------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: dict[str, Any], path: Path | None = None) -> "Watcher":
        missing = [key for key in ("id", "gmail_query") if not raw.get(key)]
        if missing:
            where = f" in {path.name}" if path else ""
            raise WatcherError(f"watcher{where} is missing: {', '.join(missing)}")

        fields_raw = raw.get("fields", {}) or {}
        if not isinstance(fields_raw, dict):
            raise WatcherError("'fields' must be a mapping of name -> spec")

        pdf = raw.get("pdf", ["render_email"])
        if isinstance(pdf, (str, dict)):
            pdf = [pdf]

        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name", raw["id"])),
            vendor=str(raw.get("vendor", raw.get("name", raw["id"]))),
            gmail_query=str(raw["gmail_query"]),
            enabled=bool(raw.get("enabled", True)),
            match=raw.get("match", {}) or {},
            fields=[FieldSpec.parse(k, v) for k, v in fields_raw.items()],
            pdf=list(pdf),
            filename=str(raw.get("filename", DEFAULT_FILENAME)),
            lookback_days=(
                int(raw["lookback_days"])
                if raw.get("lookback_days") is not None
                else None
            ),
            paid_with=str(raw.get("paid_with") or "business"),
            path=path,
        )

    @classmethod
    def from_file(cls, path: Path) -> "Watcher":
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        watcher = cls.from_dict(raw, path=path)
        watcher.source_yaml = text
        return watcher

    # ---- matching -------------------------------------------------------

    def matches(self, message: Email) -> bool:
        """Confirm a Gmail search hit really is this kind of receipt.

        The Gmail query does the coarse filtering server-side; this is the
        belt-and-braces check so a stray forwarded email or a marketing blast
        with a similar subject does not get staged as a receipt.
        """
        checks: list[tuple[str, str]] = [
            (self.match.get("from_contains", ""), message.sender),
            (self.match.get("subject_contains", ""), message.subject),
            (self.match.get("body_contains", ""), message.text),
        ]
        for needle, haystack in checks:
            if needle and needle.lower() not in haystack.lower():
                return False

        for pattern, haystack in (
            (self.match.get("subject_regex"), message.subject),
            (self.match.get("body_regex"), message.text),
        ):
            if pattern and not re.search(pattern, haystack, re.I | re.S):
                return False

        exclude = self.match.get("exclude_if_contains")
        if exclude:
            needles = [exclude] if isinstance(exclude, str) else list(exclude)
            lowered = message.searchable.lower()
            if any(n.lower() in lowered for n in needles):
                return False

        return True

    # ---- extraction -----------------------------------------------------

    def extract(self, message: Email, strict: bool = True) -> dict[str, Any]:
        """Pull the watcher's fields out of one email.

        Fields are evaluated in declaration order, and each one can reference
        the values already extracted above it via `template`. That is how the
        Trainline watcher builds a description out of origin and destination.
        """
        values: dict[str, Any] = {
            "vendor": self.vendor,
            "watcher": self.id,
            "subject": message.subject,
            "email_date": message.date_iso,
        }

        for spec in self.fields:
            try:
                values[spec.name] = self._extract_field(spec, message, values)
            except Exception as exc:  # a bad user regex should not crash a scan
                if not strict:
                    values[spec.name] = None      # left for the general rules, then for you
                    continue
                if isinstance(exc, WatcherError):
                    raise
                raise WatcherError(
                    f"[{self.id}] field '{spec.name}' failed: {exc}"
                ) from exc

        missing = [
            spec.name
            for spec in self.fields
            if spec.required and values.get(spec.name) in (None, "")
        ]
        if missing and strict:
            raise WatcherError(
                f"[{self.id}] could not find required field(s): {', '.join(missing)}"
            )
        if missing:
            values["_missing"] = missing      # strict=False: what was found, and what wasn't

        values.setdefault("purchased_on", message.date_iso)
        if not values.get("purchased_on"):
            values["purchased_on"] = message.date_iso
        return values

    def _extract_field(
        self, spec: FieldSpec, message: Email, values: dict[str, Any]
    ) -> Any:
        if spec.value is not None:
            return spec.value

        raw: str | None = None

        if spec.patterns:
            haystack = {
                "text": message.text,
                "subject": message.subject,
                "html": message.html,
                "plain": message.plain,
                # One href per line, so a pattern can pick out a booking link.
                "links": "\n".join(message.links()),
                "sender": message.sender,
                # Text inside attached PDFs, for suppliers who put the invoice
                # number in the document rather than the covering email.
                "attachment_text": message.attachment_text,
            }.get(spec.source, message.text)

            for pattern in spec.patterns:
                found = re.search(pattern, haystack, re.I | re.S)
                if not found:
                    continue
                groups = [g for g in found.groups() if g is not None]
                raw = (
                    spec.join.join(g.strip() for g in groups)
                    if groups
                    else found.group(0).strip()
                )
                raw = re.sub(r"\s+", " ", raw).strip()
                break

        if raw is None and spec.template:
            # A pure-template field: no regex of its own, composed from others.
            raw = self._render_template(spec.template, values, "")
        elif raw is not None and spec.template:
            raw = self._render_template(spec.template, values, raw)

        if raw is None or raw == "":
            raw = self._apply_default(spec, message)
            # An empty fallback (an email with no Date header, say) means
            # "not found", not "malformed". Coercing it raised, and one
            # optional field sank the whole receipt; `required` is the only
            # thing that should be allowed to do that.
            if raw is None or raw == "":
                return None

        return self._coerce(spec, str(raw))

    @staticmethod
    def _render_template(template: str, values: dict[str, Any], value: str) -> str:
        # A field that matched nothing is None, and str.format would print it
        # as the word "None" — which is how "FreeAgent subscription, None"
        # reached a filed receipt. Missing values render as nothing, and the
        # separator left dangling by a gap is tidied away.
        context = {k: ("" if v is None else v) for k, v in values.items()}
        context["value"] = value
        try:
            text = template.format(**context)
        except KeyError as exc:
            raise WatcherError(f"template refers to unknown field {exc}") from exc
        text = re.sub(r"\s{2,}", " ", text)
        return text.strip().strip(",;-–·").strip()

    @staticmethod
    def _apply_default(spec: FieldSpec, message: Email) -> Any:
        if spec.default == "email_date":
            return message.date_iso
        if spec.default == "subject":
            return message.subject
        return spec.default

    def _coerce(self, spec: FieldSpec, raw: str) -> Any:
        if spec.type == "money":
            cleaned = _MONEY.sub("", raw.replace(",", ""))
            if not cleaned:
                raise WatcherError(f"'{raw}' is not a usable amount")
            return round(float(cleaned), 2)
        if spec.type == "date":
            return _parse_date(raw, spec.date_formats)
        return raw


def _parse_date(raw: str, formats: list[str]) -> str:
    """Return an ISO date string, trying explicit formats before guessing."""
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    # ISO dates are unambiguous and must never be read day-first: dateutil
    # with dayfirst=True turned the email date 2026-09-03 into 9 March.
    iso = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", raw)
    if iso:
        try:
            return date(int(iso[1]), int(iso[2]), int(iso[3])).isoformat()
        except ValueError:
            pass
    try:
        # dayfirst matters: UK receipts write 03/09/2026 for 3 September.
        return date_parser.parse(raw, dayfirst=True, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError) as exc:
        raise WatcherError(f"'{raw}' is not a usable date") from exc


def build_filename(template: str, values: dict[str, Any]) -> str:
    """Render a receipt filename and make it safe for any filesystem."""
    context = {key: ("" if val is None else val) for key, val in values.items()}

    total = context.get("total")
    if isinstance(total, (int, float)):
        context["total"] = f"{total:.2f}"

    purchased = context.get("purchased_on")
    if isinstance(purchased, (date, datetime)):
        context["purchased_on"] = purchased.isoformat()[:10]
    context.setdefault("date", context.get("purchased_on", ""))

    try:
        rendered = template.format(**context)
    except KeyError as exc:
        raise WatcherError(f"filename template refers to unknown field {exc}") from exc

    rendered = _UNSAFE.sub("-", rendered)
    rendered = re.sub(r"\s+", " ", rendered).strip(" .-")
    if not rendered.lower().endswith(".pdf"):
        rendered += ".pdf"
    # Leave room for the numeric de-duplication suffix export.py may add.
    return rendered[:180]


def load_watchers(directory: Path, include_disabled: bool = False) -> list[Watcher]:
    """Load every watcher YAML in a directory, newest definition wins on id."""
    found: dict[str, Watcher] = {}
    errors: list[str] = []

    for path in sorted(directory.glob("*.y*ml")):
        # Hidden files are editors' temporary copies (Receipt Bridge's own
        # atomic saves among them); Python's glob, unlike a shell's, includes
        # them, and a half-written rule must never be loaded.
        if path.name.startswith("."):
            continue
        try:
            watcher = Watcher.from_file(path)
        except (WatcherError, yaml.YAMLError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if watcher.enabled or include_disabled:
            found[watcher.id] = watcher

    if errors:
        # Surface bad definitions rather than silently skipping them, but do
        # not let one broken file stop the good ones from loading.
        raise WatcherError("invalid watcher definition(s): " + "; ".join(errors))

    return list(found.values())


def load_watchers_safe(directory: Path) -> tuple[list[Watcher], list[str]]:
    """Like load_watchers, but returns errors instead of raising."""
    try:
        return load_watchers(directory), []
    except WatcherError as exc:
        watchers: list[Watcher] = []
        problems: list[str] = [str(exc)]
        for path in sorted(directory.glob("*.y*ml")):
            if path.name.startswith("."):
                continue
            try:
                watcher = Watcher.from_file(path)
                if watcher.enabled:
                    watchers.append(watcher)
            except Exception:
                continue
        return watchers, problems
