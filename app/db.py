"""SQLite storage for staged receipts.

One row per receipt, from a Gmail message or a photo. `source_id` is unique
(the Gmail message id, or `sha256:<hash>` of a photo's bytes), which is what
stops a rescan, or the same photo arriving twice, from staging it twice.
`gmail_message_id` stays unique too, for email rows.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    watcher_id        TEXT    NOT NULL,
    gmail_message_id  TEXT    UNIQUE,
    gmail_thread_id   TEXT,
    vendor            TEXT,
    reference         TEXT,
    purchased_on      TEXT,
    total             REAL,
    currency          TEXT,
    description       TEXT,
    subject           TEXT,
    email_date        TEXT,
    pdf_path          TEXT,
    pdf_source        TEXT,
    filename          TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending',
    error             TEXT,
    extra_json        TEXT,
    created_at        TEXT    NOT NULL,
    exported_at       TEXT,
    export_path       TEXT,
    account           TEXT,
    fetch_attempts    INTEGER DEFAULT 0,
    last_fetch_at     TEXT,
    source            TEXT,
    source_id         TEXT,
    paid_by           TEXT,
    vat               REAL,
    vat_number        TEXT,
    total_status      TEXT,
    photo_taken       TEXT,
    original_path     TEXT,
    category          TEXT,
    freeagent_json    TEXT,
    filed_at          TEXT,
    transaction_url   TEXT,
    native_gross      REAL,
    archived_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_receipts_status ON receipts(status);
CREATE INDEX IF NOT EXISTS idx_receipts_watcher ON receipts(watcher_id);

-- A read-only copy of FreeAgent bank transactions, for matching receipts
-- (PLAN.md §8). FreeAgent stays the record; this is refreshed from it.
CREATE TABLE IF NOT EXISTS bank_transactions (
    url                 TEXT PRIMARY KEY,
    bank_account        TEXT NOT NULL,
    dated_on            TEXT NOT NULL,
    amount              REAL NOT NULL,
    unexplained_amount  REAL,
    description         TEXT,
    is_manual           INTEGER,
    updated_at          TEXT,
    synced_at           TEXT NOT NULL,
    explanation_url     TEXT,
    explanation_attachments INTEGER,
    explanation_locked  INTEGER,
    explanation_category TEXT,
    explanation_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_bank_tx_account_date ON bank_transactions(bank_account, dated_on);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Rows the review screen shows. Anything else is history.
PENDING = "pending"
EXPORTED = "exported"
FILED = "filed"          # filed into FreeAgent by this app
IGNORED = "ignored"
FAILED = "failed"
# Deleted from the Archived page: the files are gone and the row is hidden,
# but it stays as a marker so the next scan doesn't collect the email again.
DELETED = "deleted"
ARCHIVED = (IGNORED, FAILED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns added after the first release, in order. Existing databases get
# them by ALTER TABLE, so receipts staged before a feature existed keep their
# history instead of being reset.
ADDED_COLUMNS = (
    # Which mailbox the receipt arrived at, once more than one can be
    # connected. Blank on rows staged before multi-account support.
    ("account", "TEXT"),
    # Automatic retries of a supplier document that wasn't available first
    # time: how many, and when the last one ran.
    ("fetch_attempts", "INTEGER DEFAULT 0"),
    ("last_fetch_at", "TEXT"),
    # Phone photos (PLAN.md §4.2, §10). `source` is "gmail" or "photo".
    ("source", "TEXT"),
    ("source_id", "TEXT"),
    ("paid_by", "TEXT"),          # "business", "personal", or NULL: not asked
    ("vat", "REAL"),              # printed UK VAT
    ("vat_number", "TEXT"),
    ("total_status", "TEXT"),     # confirmed / unconfirmed / missing
    ("photo_taken", "TEXT"),      # EXIF capture time
    ("original_path", "TEXT"),    # the untouched original in data/photos/
    # Filing (PLAN.md §9): the FreeAgent category URL, what was created
    # there (for undo and recovery), and when.
    ("category", "TEXT"),
    ("freeagent_json", "TEXT"),
    ("filed_at", "TEXT"),
    # The bank payment you chose for a receipt ("This one", "Use this",
    # Change payment), and for a foreign expense the £ actually charged.
    ("transaction_url", "TEXT"),
    ("native_gross", "REAL"),
    # When a receipt was ignored or couldn't be read, for "delete archived
    # receipts after N days". Older archived rows fall back to created_at.
    ("archived_at", "TEXT"),
)


# What the Statement shows (and can change) of FreeAgent's own explanation.
EXPLANATION_FIELDS = ("category", "sales_tax_rate", "project", "rebill_type", "rebill_factor",
                      "marked_for_review")       # false: approved in FreeAgent
# Bumped when the cached transactions gain a field: the next read is a full one.
BANK_CACHE_VERSION = "2"


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        self._migrate()

    def wipe(self) -> None:
        """Empty every table at once (Reset app). In place, in one
        transaction: anything reading meanwhile sees all of it or none,
        never a database without its tables."""
        with self.connect() as conn:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                conn.execute(f'DELETE FROM "{table}"')
        with self.connect() as conn:
            conn.execute("VACUUM")

    def _migrate(self) -> None:
        """Bring an existing database up to the current shape."""
        with self.connect() as conn:
            info = {row[1]: row for row in conn.execute("PRAGMA table_info(receipts)")}
            needs_rebuild = bool(info["gmail_message_id"][3])     # still NOT NULL
        if needs_rebuild:
            self._allow_rows_without_gmail()
        with self.connect() as conn:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(receipts)")}
            for column, kind in ADDED_COLUMNS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE receipts ADD COLUMN {column} {kind}")
            # Already explained in FreeAgent (a bank rule, an accepted guess):
            # one explanation, and whether a receipt is attached to it yet.
            have = {row[1] for row in conn.execute("PRAGMA table_info(bank_transactions)")}
            for column, kind in (("explanation_url", "TEXT"), ("explanation_attachments", "INTEGER"),
                                 ("explanation_locked", "INTEGER"), ("explanation_category", "TEXT"),
                                 ("explanation_json", "TEXT")):
                if column not in have:
                    conn.execute(f"ALTER TABLE bank_transactions ADD COLUMN {column} {kind}")
                    # cached rows lack it: make the next FreeAgent read a full one
                    conn.execute("DELETE FROM state WHERE key = 'freeagent:last_sync'")
            seen = conn.execute("SELECT value FROM state WHERE key = 'bank_cache_version'").fetchone()
            if (seen[0] if seen else None) != BANK_CACHE_VERSION:
                conn.execute("DELETE FROM state WHERE key = 'freeagent:last_sync'")
                conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('bank_cache_version', ?)",
                             (BANK_CACHE_VERSION,))
            # Email rows predate `source`: their identity is the Gmail id.
            conn.execute(
                "UPDATE receipts SET source = 'gmail', source_id = gmail_message_id "
                "WHERE source_id IS NULL AND gmail_message_id IS NOT NULL"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_source_id ON receipts(source_id)"
            )

    def _allow_rows_without_gmail(self) -> None:
        """One-off rebuild so `gmail_message_id` can be empty (photo rows).

        SQLite can't drop NOT NULL in place, so the table is copied into the
        new shape inside one transaction: it either all happens or none of
        it does. A copy of the database is saved first regardless.
        """
        backup = self.path.parent / "backups" / (
            f"receipts-before-photos-{datetime.now():%Y%m%d-%H%M%S}.sqlite3")
        self.backup_to(backup)
        new_table = SCHEMA.split("CREATE TABLE IF NOT EXISTS receipts (", 1)[1].split(");", 1)[0]
        with self.connect() as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(receipts)")]
            listed = ", ".join(columns)
            conn.executescript(f"""
                BEGIN;
                CREATE TABLE receipts_rebuilt ({new_table});
                INSERT INTO receipts_rebuilt ({listed}) SELECT {listed} FROM receipts;
                DROP TABLE receipts;
                ALTER TABLE receipts_rebuilt RENAME TO receipts;
                CREATE INDEX IF NOT EXISTS idx_receipts_status ON receipts(status);
                CREATE INDEX IF NOT EXISTS idx_receipts_watcher ON receipts(watcher_id);
                COMMIT;
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # A scan writes while the UI reads. WAL lets readers proceed without
        # waiting on the writer, and the busy timeout turns the occasional
        # overlap into a short wait instead of a "database is locked" error.
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- receipts -------------------------------------------------------

    def has_message(self, gmail_message_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM receipts WHERE gmail_message_id = ?",
                (gmail_message_id,),
            ).fetchone()
        return row is not None

    def has_source(self, source_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM receipts WHERE source_id = ?", (source_id,)
            ).fetchone()
        return row is not None

    def with_total(self, total: float, exclude_id: int | None = None) -> list[sqlite3.Row]:
        """Receipts for the same amount: candidates for a duplicate check."""
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT * FROM receipts WHERE total IS NOT NULL AND ABS(total - ?) < 0.005 "
                "AND id IS NOT ? AND status != 'deleted' ORDER BY id",
                (total, exclude_id),
            ).fetchall())

    def update_receipt(self, receipt_id: int, changes: dict[str, Any]) -> None:
        """Overwrite some fields of a receipt (re-reading a photo)."""
        if not changes:
            return
        payload = dict(changes)
        if isinstance(payload.get("extra_json"), (dict, list)):
            payload["extra_json"] = json.dumps(payload["extra_json"])
        sets = ", ".join(f"{key} = :{key}" for key in payload)
        payload["_id"] = receipt_id
        with self.connect() as conn:
            conn.execute(f"UPDATE receipts SET {sets} WHERE id = :_id", payload)

    def has_reference(self, watcher_id: str, reference: str) -> bool:
        """Has this supplier's invoice/transaction number already been staged?

        Message ids are not enough on their own. `ingest` on a saved .eml keys
        on the RFC Message-ID header while `scan` keys on Gmail's own id, so
        the same email arriving by both routes looks like two receipts. An
        invoice number is the supplier's identity for the purchase and does
        not change with how the email reached us.
        """
        if not reference:
            return False
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM receipts WHERE watcher_id = ? AND reference = ?",
                (watcher_id, reference),
            ).fetchone()
        return row is not None

    def insert_receipt(self, data: dict[str, Any]) -> int:
        """Stage a receipt. Returns its id, or 0 if it was already staged.

        Callers check `has_message` first, but that check and this insert are
        not atomic: the menu-bar app's automatic scan and a scan started by
        hand can be walking the same mailbox at once, and whichever loses the
        race would otherwise crash on the unique index. Collecting the same
        receipt twice is the thing worth preventing, not an error worth
        raising, so a clash is simply a no-op.
        """
        payload = dict(data)
        payload.setdefault("status", PENDING)
        if payload["status"] in ARCHIVED:
            payload.setdefault("archived_at", _now())
        if payload.get("gmail_message_id") and not payload.get("source_id"):
            payload["source"] = "gmail"
            payload["source_id"] = payload["gmail_message_id"]
        payload["created_at"] = _now()
        if isinstance(payload.get("extra_json"), (dict, list)):
            payload["extra_json"] = json.dumps(payload["extra_json"])
        columns = ", ".join(payload)
        placeholders = ", ".join(f":{key}" for key in payload)
        with self.connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO receipts ({columns}) VALUES ({placeholders}) "
                "ON CONFLICT DO NOTHING",
                payload,
            )
            # rowcount is 0 when the conflict clause swallowed the insert.
            return int(cursor.lastrowid) if cursor.rowcount else 0

    def list_receipts(self, status: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM receipts"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        else:
            query += " WHERE status != 'deleted'"
        query += " ORDER BY COALESCE(purchased_on, email_date) DESC, id DESC"
        with self.connect() as conn:
            return list(conn.execute(query, params).fetchall())

    def get_receipt(self, receipt_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM receipts WHERE id = ?", (receipt_id,)
            ).fetchone()

    def set_status(self, receipt_id: int, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE receipts SET status = ?, archived_at = CASE WHEN ? THEN ? END WHERE id = ?",
                (status, status in ARCHIVED, _now(), receipt_id),
            )

    def archived_ids(self, before: str | None = None) -> list[int]:
        """Ignored and unreadable receipts, optionally only those archived
        before an ISO time."""
        query = f"SELECT id FROM receipts WHERE status IN ({', '.join('?' for _ in ARCHIVED)})"
        params: list[Any] = list(ARCHIVED)
        if before:
            query += " AND COALESCE(archived_at, created_at) < ?"
            params.append(before)
        with self.connect() as conn:
            return [row[0] for row in conn.execute(query, params).fetchall()]

    def path_in_use(self, path: str, exclude_id: int) -> bool:
        """Does another live receipt still point at this file?"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM receipts WHERE id != ? AND status != 'deleted' "
                "AND (pdf_path = ? OR original_path = ?)",
                (exclude_id, path, path),
            ).fetchone()
        return row is not None

    def mark_exported(self, receipt_id: int, export_path: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE receipts SET status = ?, exported_at = ?, export_path = ? "
                "WHERE id = ?",
                (EXPORTED, _now(), export_path, receipt_id),
            )

    def delete_receipt(self, receipt_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))

    def note_fetch_attempt(self, receipt_id: int, when: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE receipts SET fetch_attempts = COALESCE(fetch_attempts, 0) + 1, "
                "last_fetch_at = ? WHERE id = ?",
                (when, receipt_id),
            )

    def retry_candidates(
        self, watcher_ids: set[str], max_attempts: int, older_than: str
    ) -> list[int]:
        """Pending email-copy receipts whose supplier offers something better.

        `created_at` stands in for the first attempt, so a receipt that has
        only just fallen back isn't retried moments later.
        """
        if not watcher_ids:
            return []
        marks = ",".join("?" * len(watcher_ids))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM receipts WHERE status = 'pending' "
                f"AND pdf_source = 'rendered_email' AND watcher_id IN ({marks}) "
                f"AND COALESCE(fetch_attempts, 0) < ? "
                f"AND COALESCE(last_fetch_at, created_at) < ? ORDER BY id",
                (*sorted(watcher_ids), max_attempts, older_than),
            ).fetchall()
        return [row[0] for row in rows]

    def backup_to(self, target: Path) -> None:
        """Consistent copy of the live database, safe while it is in use."""
        target.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.path)
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()

    def delete_state(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM state WHERE key = ?", (key,))

    def update_pdf(self, receipt_id: int, pdf_path: str, pdf_source: str) -> None:
        """Swap in a better document for an existing receipt."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE receipts SET pdf_path = ?, pdf_source = ?, error = NULL "
                "WHERE id = ?",
                (pdf_path, pdf_source, receipt_id),
            )

    def totals(self, status: str) -> dict[str, float]:
        """Sum of amounts in one status, per currency."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(currency, ''), SUM(total) FROM receipts "
                "WHERE status = ? AND total IS NOT NULL GROUP BY currency",
                (status,),
            ).fetchall()
        return {row[0]: round(float(row[1] or 0), 2) for row in rows}

    def list_state(self, prefix: str = "") -> dict[str, str]:
        """Every stored state key, optionally filtered by prefix."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM state WHERE key LIKE ?", (f"{prefix}%",)
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def count_for_account(self, account: str) -> int:
        """How many receipts arrived at one mailbox.

        Rows staged before multi-account support have NULL here rather than
        an address, so they are counted under the empty string.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM receipts "
                "WHERE COALESCE(account, '') = ? AND status != 'deleted'",
                (account,),
            ).fetchone()
        return int(row[0]) if row else 0

    def counts_by_status(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM receipts GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    # ---- FreeAgent bank transactions (read-only copy) --------------------

    def save_bank_transactions(self, transactions: list[dict[str, Any]]) -> int:
        now = _now()
        rows = []
        for t in transactions:
            explanations = [e for e in t.get("bank_transaction_explanations") or [] if isinstance(e, dict)]
            only = explanations[0] if len(explanations) == 1 else None
            rows.append((
                t["url"], t["bank_account"], t["dated_on"], float(t["amount"]),
                float(t["unexplained_amount"]) if t.get("unexplained_amount") is not None else None,
                t.get("description") or t.get("full_description") or "",
                1 if t.get("is_manual") else 0, t.get("updated_at"), now,
                only.get("url") if only else None,
                len(only.get("attachments") or []) + (1 if only.get("attachment") else 0) if only else None,
                1 if only and only.get("is_locked") else 0,
                only.get("category") if only else None,
                # FreeAgent's own VAT rate and re-billing on it, for the Statement
                json.dumps({k: only.get(k) for k in EXPLANATION_FIELDS}) if only else None,
            ))
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO bank_transactions (url, bank_account, dated_on, amount, "
                "unexplained_amount, description, is_manual, updated_at, synced_at, "
                "explanation_url, explanation_attachments, explanation_locked, explanation_category, explanation_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
                "bank_account = excluded.bank_account, dated_on = excluded.dated_on, "
                "amount = excluded.amount, unexplained_amount = excluded.unexplained_amount, "
                "description = excluded.description, is_manual = excluded.is_manual, "
                "updated_at = excluded.updated_at, synced_at = excluded.synced_at, "
                "explanation_url = excluded.explanation_url, "
                "explanation_attachments = excluded.explanation_attachments, "
                "explanation_locked = excluded.explanation_locked, "
                "explanation_category = excluded.explanation_category, "
                "explanation_json = excluded.explanation_json",
                rows,
            )
        return len(rows)

    def bank_transactions(self, bank_accounts: list[str]) -> list[sqlite3.Row]:
        if not bank_accounts:
            return []
        marks = ",".join("?" * len(bank_accounts))
        with self.connect() as conn:
            return list(conn.execute(
                f"SELECT * FROM bank_transactions WHERE bank_account IN ({marks}) "
                "ORDER BY dated_on, url", tuple(bank_accounts),
            ).fetchall())

    def clear_bank_transactions(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM bank_transactions")

    # ---- key/value state ------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
