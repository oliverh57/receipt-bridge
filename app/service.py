"""The one long-lived owner of everything slow.

The rule this module exists to enforce: **nothing the UI asks for ever waits
on the network.** The previous design called Gmail while rendering pages, so
every click paid a round-trip to Google and the first window took sixteen
seconds to draw. Here, anything slow — scanning, retrying a receipt, checking
whether an account still works — runs on a single background worker, and the
UI only ever reads a snapshot of in-memory state plus a local database.

One worker, not several, because the slow jobs share resources that do not
like company: a headless browser, Gmail clients that are not thread-safe, and
a supplier website that rate-limits when it is hit in parallel. Jobs queue and
run in order; the UI shows which one is current.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from . import login_item, setup_guide, updates
from .accounts import AccountStore, adopt_legacy_scan_state
from .config import Config
from .db import DELETED, EXPORTED, FAILED, FILED, IGNORED, PENDING, Database
from .filer import FilingError, file_receipt, plan_for, unfile
from .export import ExportResult, export_receipts
from .freeagent import Credentials, FreeAgent, FreeAgentError, NotConnected, TokenStore, VAT_SCHEMES, vat_settings
from .gmail_client import SignInCancelled
from .matcher import match_receipts, needs_receipt, supplier_token
from .review import Context, explained_for_good, is_undated, review
IMAGE_TYPES = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'}
from .photo_inbox import pending_files, process_inbox, reread
from .watchers import build_filename, load_watchers_safe

log = logging.getLogger(__name__)

# How often account health is re-checked in the background, and how often an
# automatic scan runs. Both are cheap enough to be generous.
HEALTH_INTERVAL = timedelta(minutes=30)
DEFAULT_AUTO_SCAN = timedelta(hours=6)
AUTO_RETRY_GAP = timedelta(hours=12)
AUTO_RETRY_LIMIT = 3
# Which review flags a correction to each field settles (app/photo_inbox.py
# writes them). Matched on wording, so keep the two in step.
FLAG_WORDS = {
    "total": ("Total", "totals"),
    "currency": ("currency",),
    "purchased_on": ("date", "Date", "18 months", "photo was taken"),
    "vendor": ("Supplier name",),
    "paid_by": ("personally?",),
}
# An undated receipt's date, borrowed from the payment you chose for it.
# Contains "Date", so typing the date in clears it (FLAG_WORDS).
DATE_FROM_PAYMENT = "Date taken from the payment you chose. Check it against the receipt."
INBOX_WARN_BYTES = 500 * 1024 * 1024   # warn if this much is stuck in the inbox
FREEAGENT_SYNC_SECONDS = 3600      # re-read FreeAgent hourly while connected
FREEAGENT_RETRY_SECONDS = 120      # sooner while it couldn't be reached
# "Use that email": payments this recent are looked for in Gmail, each once a
# day for up to two weeks, and at most this many searches per run.
EMAIL_HINT_DAYS = 60
EMAIL_HINT_RECHECK_DAYS = 14
EMAIL_HINT_SEARCHES = 25
# The Emails view: emails per page of the list, and whole emails kept in
# memory so opening one again (or adding it) doesn't download it twice.
EMAIL_PAGE = 50
EMAIL_RECEIPT_PAGES = 4          # "Likely receipts": Gmail pages read for one page of the list
EMAILS_CACHED = 12
# Category suggestions asked of the on-device model per run.
CATEGORY_GUESSES = 20
FREEAGENT_HISTORY_DAYS = 120       # how far back the first read of an account goes
BACKUPS_KEPT = 7
UPDATE_CHECK_INTERVAL = timedelta(days=1)   # GitHub is asked at most this often unprompted


def _freeagent_failure(exc: Exception) -> tuple[str, str]:
    """A failed FreeAgent read, in words for the person (the detail is in the
    log), and its kind: "offline" and "unavailable" fix themselves."""
    text = str(exc)
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return ("Check this Mac is online. It tries again by itself.", "offline")
    if re.search(r"failed \(5\d\d\)", text) or "rate limiting" in text:
        return ("FreeAgent is having problems at its end. It tries again by itself.", "unavailable")
    return (f"Couldn't read FreeAgent: {text}", "error")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Health:
    """The last known state of one Gmail account. Never computed on demand."""

    state: str = "unknown"  # ok | expired | signed_out | error | unknown
    detail: str = ""
    checked_at: str | None = None


@dataclass
class Activity:
    """What the background worker is doing, in words a person can read."""

    busy: bool = False
    kind: str = ""  # scan | retry | connect | health | photos
    label: str = ""  # "Checking Trainline"
    current: int = 0
    total: int = 0
    started_at: str | None = None
    log: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    """How the most recent job ended."""

    kind: str
    ok: bool
    message: str
    finished_at: str
    found: int = 0


class ReceiptService:
    def __init__(self, config: Config, notifier: Any = None):
        self.config = config
        config.ensure_dirs()
        if any(config.setup_folders):
            setup_guide.use_folders(*config.setup_folders)
        self.db = Database(config.db_path)
        self.accounts = AccountStore(config)
        adopt_legacy_scan_state(self.db, self.accounts)

        # Posts system notifications. The macOS shell supplies one (with a
        # `status` and `send`); the CLI and tests run without.
        self.notifier = notifier
        self.notify = notifier.send if notifier is not None else None

        self._lock = threading.RLock()
        self._jobs: queue.Queue[tuple[str, Callable[[], None]]] = queue.Queue()
        self._queued: set[str] = set()
        self._activity = Activity()
        self._outcome: Outcome | None = None
        self._health: dict[str, Health] = {}
        self._connecting = False
        self._email_cache: dict[tuple[str, str], Any] = {}   # the Emails view, most recent last
        self._last_log: list[str] = []
        # Bumped on every change, so the UI can skip redrawing when nothing
        # happened since it last looked.
        self._version = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # OAuth `state` values handed out for FreeAgent sign-in: value → expiry.
        self._freeagent_states: dict[str, float] = {}
        self._inbox_bytes = 0          # measured by the scheduler, not on every UI poll
        self._file_results: dict[str, Any] | None = None   # the last File / File all, for its result list
        self._freeagent_error = ""
        # what kind of failure it is: "offline" and "unavailable" fix
        # themselves (retried every couple of minutes); "error" needs looking at
        self._freeagent_problem = ""
        self._review_counts_cache: tuple[Any, dict[str, int]] | None = None
        self._checking_updates = False
        # Quits and reopens the app after an update; the macOS shell sets it.
        # Returns False when it can't (not running as the app).
        self.restart: Callable[[], bool] | None = None

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._backup_database()
        for target, name in ((self._work, "worker"), (self._schedule, "scheduler")):
            thread = threading.Thread(target=target, name=f"receipt-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)
        self._adopt_setup_state()
        setup_guide.write_shortcut_settings(self.photo_inbox)
        self.check_accounts()
        self.reread_partly_read_emails()
        self.backfill_layouts()
        self.backfill_tidy()

    def _backup_database(self) -> None:
        """Keep a week of daily copies of the database.

        It is the record of what has been filed. The PDFs survive in the
        export folders regardless, but losing this would mean losing track
        of which receipts went to FreeAgent and when.
        """
        try:
            folder = self.config.data_dir / "backups"
            today = folder / f"receipts-{datetime.now():%Y-%m-%d}.sqlite3"
            if not today.exists():
                self.db.backup_to(today)
            for old in sorted(folder.glob("receipts-*.sqlite3"))[:-BACKUPS_KEPT]:
                old.unlink()
        except Exception:
            log.exception("database backup failed")

    def stop(self) -> None:
        self._stop.set()
        self._jobs.put(("stop", lambda: None))

    def _bump(self) -> None:
        with self._lock:
            self._version += 1

    # ---- the worker -----------------------------------------------------

    def _enqueue(self, key: str, job: Callable[[], None]) -> bool:
        """Queue a job unless an identical one is already waiting or running."""
        with self._lock:
            if key in self._queued:
                return False
            self._queued.add(key)
        self._jobs.put((key, job))
        self._bump()
        return True

    def _work(self) -> None:
        while not self._stop.is_set():
            key, job = self._jobs.get()
            if self._stop.is_set():
                return
            try:
                job()
            except Exception:
                # A job blowing up must never take the worker with it, or the
                # app silently stops scanning forever.
                log.exception("background job %s failed", key)
            finally:
                with self._lock:
                    self._queued.discard(key)
                    # Keep the finished job's log for the Settings screen;
                    # health checks are too routine to be worth showing.
                    if self._activity.log and self._activity.kind != "health":
                        self._last_log = list(self._activity.log)
                    self._activity = Activity()
                self._bump()

    def _schedule(self) -> None:
        """Periodic health checks, automatic scans, and the photo inbox."""
        last_health = time.monotonic()
        last_freeagent = 0.0
        last_purge = 0.0
        while not self._stop.wait(60):
            self.check_photos()
            size = self.inbox_size()
            if size > INBOX_WARN_BYTES >= self._inbox_bytes:
                self.notify_about("problems", f"{size // (1024 * 1024)} MB is waiting in the receipt inbox. "
                                  "Something may be stuck: check Settings → Receipt inbox.")
            self._inbox_bytes = size
            wait = FREEAGENT_RETRY_SECONDS if self._freeagent_problem in ("offline", "unavailable") \
                else FREEAGENT_SYNC_SECONDS
            if time.monotonic() - last_freeagent >= wait and self._freeagent_connected():
                self.sync_freeagent()
                last_freeagent = time.monotonic()
            if time.monotonic() - last_health >= HEALTH_INTERVAL.total_seconds():
                self.check_accounts()
                last_health = time.monotonic()

            self.flush_notifications()
            if self._update_check_due():
                self.check_for_updates()
            if time.monotonic() - last_purge >= 3600:
                self.purge_archived()
                last_purge = time.monotonic()

            due = self._auto_scan_due()
            if due:
                self.scan(reason="automatic")

    def _auto_scan_due(self) -> bool:
        if not self.db.get_state("pref:auto_scan", "1") == "1":
            return False
        last = self.db.get_state("last_scan_finished_at")
        if not last:
            return True
        try:
            when = datetime.fromisoformat(last)
        except ValueError:
            return True
        hours = float(self.config.raw.get("auto_scan_hours", 6) or 6)
        return datetime.now(timezone.utc) - when >= timedelta(hours=hours)

    def _set_activity(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self._activity, key, value)
        self._bump()

    def _note(self, line: str) -> None:
        with self._lock:
            self._activity.log.append(line)
            del self._activity.log[:-200]
        self._bump()

    def _finish(self, kind: str, ok: bool, message: str, found: int = 0) -> None:
        with self._lock:
            self._outcome = Outcome(kind, ok, message, _now(), found)
        self._bump()

    # ---- scanning -------------------------------------------------------

    def scan(self, reason: str = "manual") -> bool:
        return self._enqueue("scan", lambda: self._run_scan(reason))

    def check_now(self) -> list[str]:
        """"Check now": the photo inbox, FreeAgent and (when connected) email,
        each queued as usual. Returns what was queued."""
        queued = []
        if self.check_photos():
            queued.append("photos")
        if self._freeagent_connected() and self.sync_freeagent():
            queued.append("freeagent")
        if self.accounts.list() and self.scan():
            queued.append("email")
        return queued

    def _run_scan(self, reason: str) -> None:
        from .pipeline import scan as run_pipeline

        self._set_activity(
            busy=True, kind="scan", label="Checking your inbox", started_at=_now(), log=[]
        )
        watchers, problems = load_watchers_safe(self.config.watchers_dir)
        for problem in problems:
            self._note(f"supplier problem: {problem}")

        usable = [
            (account, client)
            for account, client in self.accounts.clients()
            if self._health.get(account.email, Health()).state not in ("expired", "signed_out")
        ]
        if not usable:
            self._finish(
                "scan",
                False,
                "No working Gmail account. Reconnect one in Settings.",
            )
            return

        def on_event(event: dict[str, Any]) -> None:
            if event["type"] == "supplier":
                self._set_activity(
                    label=f"Checking {event['supplier']}",
                    current=0,
                    total=event["total"],
                )
            elif event["type"] == "item":
                self._set_activity(current=event["position"], total=event["total"])

        report = run_pipeline(
            self.config,
            self.db,
            watchers,
            progress=self._note,
            accounts=[(a.email, c) for a, c in usable],
            on_event=on_event,
        )
        self.db.set_state("last_scan_finished_at", _now())

        if report.staged:
            message = f"Found {report.staged} new receipt{'s' if report.staged != 1 else ''}"
        else:
            message = "No new receipts"
        if report.failed:
            message += f" · {report.failed} couldn't be read"

        self._finish("scan", report.failed == 0, message, found=report.staged)
        if report.staged:
            self.notify_about("new", f"{message}. Ready to review.")

        # A scan is also the best evidence of whether an account works.
        self.check_accounts()
        self.backfill_layouts()
        self.guess_categories()
        self.auto_link()
        self.find_emails()
        queued = self._queue_automatic_retries()
        if queued:
            log.info("queued %d automatic retr%s", queued, "y" if queued == 1 else "ies")

    # ---- phone photos -----------------------------------------------------

    # Where to look for receipts and where read originals go: per-user
    # settings (PLAN.md §12), stored in the app's data, never in code.

    @property
    def photo_inbox(self) -> Path:
        chosen = self.db.get_state("pref:photo_inbox")
        return Path(chosen) if chosen else self.config.photo_inbox

    @property
    def photo_archive(self) -> Path:
        chosen = self.db.get_state("pref:photo_archive")
        return Path(chosen) if chosen else self.config.photos_dir

    def set_folders(self, inbox: str | None = None, archive: str | None = None) -> None:
        """Validate and save folder choices. The inbox must exist; the archive
        is created if needed. Neither may sit inside the other, or the app
        would read its own archive as new receipts."""
        new_inbox = Path(os.path.expanduser(inbox)).resolve() if inbox else self.photo_inbox
        new_archive = Path(os.path.expanduser(archive)).resolve() if archive else self.photo_archive
        for path in (new_inbox, new_archive):
            if not path.is_absolute():
                raise ValueError("Choose a full folder path")
        if inbox and not new_inbox.is_dir():
            raise ValueError(f"No folder at {new_inbox}")
        if new_inbox == new_archive or new_inbox in new_archive.parents or new_archive in new_inbox.parents:
            raise ValueError("The inbox and the archive must be separate folders, neither inside the other")
        if archive:
            new_archive.mkdir(parents=True, exist_ok=True)
            self.db.set_state("pref:photo_archive", str(new_archive))
        if inbox:
            previous = self.photo_inbox
            self.db.set_state("pref:photo_inbox", str(new_inbox))
            setup_guide.write_shortcut_settings(new_inbox, previous)
        self._bump()
        self.check_photos()

    def reset_folder(self, which: str) -> None:
        if which not in ("inbox", "archive"):
            raise ValueError("which must be inbox or archive")
        previous = self.photo_inbox
        self.db.delete_state(f"pref:photo_{which}")
        if which == "inbox":
            setup_guide.write_shortcut_settings(self.photo_inbox, previous)
        self._bump()

    def create_inbox_folders(self) -> None:
        """Bank/ and Expense/ inside the inbox: the Shortcut's two answers."""
        for sub in setup_guide.SUBFOLDERS:
            (self.photo_inbox / sub).mkdir(parents=True, exist_ok=True)
        setup_guide.write_shortcut_settings(self.photo_inbox)
        self._bump()

    def setup_inbox(self, location: str) -> Path:
        """The setup guide's "where are photos saved?": "icloud" or a folder
        from the picker. Makes the inbox with Bank/ and Expense/ in it, and
        points the app (and the iPhone Shortcut) at it."""
        inbox = setup_guide.inbox_for(location).resolve()
        archive = self.photo_archive.resolve()
        if inbox == archive or inbox in archive.parents or archive in inbox.parents:
            raise ValueError("That's where read receipts are archived. Choose another folder.")
        for sub in setup_guide.SUBFOLDERS:
            (inbox / sub).mkdir(parents=True, exist_ok=True)
        previous = self.photo_inbox
        if inbox == self.config.photo_inbox.resolve():
            self.db.delete_state("pref:photo_inbox")
        else:
            self.db.set_state("pref:photo_inbox", str(inbox))
        setup_guide.write_shortcut_settings(inbox, previous)
        self._bump()
        self.check_photos()
        return inbox

    # ---- first-run setup guide -------------------------------------------

    def _adopt_setup_state(self) -> None:
        """Copies set up before the guide existed never see it: FreeAgent
        already connected means someone has been through setup."""
        if self.db.get_state("pref:setup_done") is None and self.config.freeagent_token_file.exists():
            self.db.set_state("pref:setup_done", "1")

    def set_setup_done(self, done: bool) -> None:
        self.db.set_state("pref:setup_done", "1" if done else "0")
        self._bump()

    def setup_snapshot(self) -> dict[str, Any]:
        inbox = self.photo_inbox
        return {
            "done": self.db.get_state("pref:setup_done") == "1",
            "icloud": setup_guide.icloud_available(),
            "icloud_inbox": setup_guide.icloud_relative(inbox),     # None: not in iCloud Drive
            # the shared Shortcut's fixed folder in iCloud Drive
            "shortcut_saves_to": setup_guide.SHORTCUT_SAVES_TO,
            "shortcut_url": self.config.shortcut_url,
        }

    def inbox_size(self) -> int:
        """Bytes waiting in the receipt inbox. It should stay near zero: files
        are moved out as soon as they're read. A big number means something
        is stuck, and on iCloud's free 5 GB that matters (PLAN.md §4.2)."""
        total = 0
        try:
            for path in self.photo_inbox.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
        except OSError:
            pass
        return total

    def check_photos(self) -> bool:
        """Queue a pass over the iCloud photo inbox, if anything is in it.

        Listing the folder is cheap, so this runs every minute; the reading
        happens on the worker like everything else slow.
        """
        try:
            waiting = pending_files(self.photo_inbox)
        except OSError:
            log.debug("photo inbox not readable", exc_info=True)
            return False
        if not waiting:
            return False
        return self._enqueue("photos", self._run_photos)

    def _run_photos(self) -> None:
        self._set_activity(busy=True, kind="photos", label="Reading receipt photos", started_at=_now(), log=[])
        report = process_inbox(self.config, self.db, note=self._note,
                               inbox=self.photo_inbox, archive_dir=self.photo_archive)
        if not (report.staged or report.failed):
            return
        n = report.found
        message = f"{n} photo{'s' if n != 1 else ''} added" if n else "No photos added"
        if report.failed:
            message += f" · {len(report.failed)} couldn't be read"
        self._finish("photos", not report.failed, message, found=n)
        self.backfill_layouts()
        self.guess_categories()
        self.auto_link()
        if n:
            self.notify_about("new", f"{message}. Ready to review.")

    # ---- FreeAgent (read-only in Phase 2) ---------------------------------

    def _freeagent(self) -> FreeAgent | None:
        try:
            credentials = Credentials.load(self.config.freeagent_credentials_file,
                                           self.config.freeagent_redirect_uri)
        except (FreeAgentError, ValueError) as exc:
            self._freeagent_error = str(exc)
            return None
        if credentials is None:
            return None
        return FreeAgent(credentials, TokenStore(self.config.freeagent_token_file))

    def _freeagent_connected(self) -> bool:
        client = self._freeagent()
        return bool(client and client.connected)

    def connect_freeagent(self) -> str:
        """Start sign-in: a one-time `state`, and FreeAgent's approval page
        opened in the user's own browser. Returns the URL too."""
        client = self._freeagent()
        if client is None:
            raise FreeAgentError("This copy is missing its FreeAgent key (freeagent_credentials.json). "
                                 "Update from Settings → General, or run the installer again.")
        state = secrets.token_urlsafe(24)
        with self._lock:
            now = time.monotonic()
            self._freeagent_states = {k: v for k, v in self._freeagent_states.items() if v > now}
            self._freeagent_states[state] = now + 600          # ten minutes to sign in
        url = client.authorize_url(state)
        subprocess.run(["open", url], check=False)
        return url

    def freeagent_callback(self, code: str, state: str) -> bool:
        """FreeAgent sent the browser back. Accept only a state we issued, once.
        The code is exchanged on the worker: request handlers never call out."""
        with self._lock:
            expiry = self._freeagent_states.pop(state, None)
        if not code or expiry is None or expiry < time.monotonic():
            return False
        return self._enqueue("freeagent-connect", lambda: self._finish_freeagent_connect(code))

    def _finish_freeagent_connect(self, code: str) -> None:
        self._set_activity(busy=True, kind="connect", label="Connecting FreeAgent", started_at=_now(), log=[])
        client = self._freeagent()
        try:
            client.exchange_code(code)
            self._freeagent_error = ""
        except FreeAgentError as exc:
            self._freeagent_error = str(exc)
            self._finish("connect", False, f"FreeAgent sign-in didn't complete: {exc}")
            return
        self._run_freeagent_sync()
        self._finish("connect", True, "Connected FreeAgent")

    def disconnect_freeagent(self) -> None:
        client = self._freeagent()
        if client:
            client.disconnect()
        self.db.clear_bank_transactions()
        for key in ("freeagent:reference", "freeagent:last_sync", "freeagent:accounts"):
            self.db.delete_state(key)
        self._bump()

    # Remembered per FreeAgent company: their URLs mean nothing in another.
    COMPANY_STATE = ("category_for:", "auto_link:", "no_receipt:", "email_hint:", "payment:", "explained:")

    def _check_company(self, company: str) -> None:
        """Switching company (sandbox → your real books, say): forget
        everything that points into the old one, so nothing made in one is
        sent to the other. Receipts stay; their category, chosen payment,
        re-billing, bank account and dry runs are cleared. Filed ones keep
        their record."""
        before = self.db.get_state("freeagent:company")
        if before == company:
            return
        self.db.set_state("freeagent:company", company)
        if before is None:
            return
        log.info("FreeAgent company changed (%s → %s): clearing the old company's links", before, company)
        self.db.clear_bank_transactions()
        for key in ("freeagent:accounts", "freeagent:reference"):
            self.db.delete_state(key)
        for prefix in self.COMPANY_STATE:
            for key in self.db.list_state(prefix):
                self.db.delete_state(key)
        for status in (PENDING, IGNORED, FAILED):
            for row in self.db.list_receipts(status):
                extra = json.loads(row["extra_json"] or "{}")
                dropped = [k for k in ("rebill", "bank_account", "category_guess") if extra.pop(k, None) is not None]
                if row["category"] or row["transaction_url"] or row["freeagent_json"] or dropped:
                    self.db.update_receipt(row["id"], {"category": None, "transaction_url": None,
                                                       "freeagent_json": None, "extra_json": extra})

    def set_freeagent_accounts(self, urls: list[str]) -> None:
        """The business bank accounts to match against, picked from FreeAgent's
        list (never assumed: PLAN.md §12)."""
        known = {a["url"] for a in self._freeagent_reference().get("bank_accounts", [])}
        chosen = [u for u in urls if u in known]
        self.db.set_state("freeagent:accounts", json.dumps(chosen))
        self.db.delete_state("freeagent:last_sync")          # fetch the new ones in full
        self._bump()
        self.sync_freeagent()

    def _freeagent_accounts(self) -> list[str]:
        try:
            return json.loads(self.db.get_state("freeagent:accounts") or "[]")
        except ValueError:
            return []

    def _freeagent_reference(self) -> dict[str, Any]:
        try:
            reference = json.loads(self.db.get_state("freeagent:reference") or "{}")
        except ValueError:
            return {}
        # FreeAgent only reports the scheme registered with; a correction
        # made in Settings wins, everywhere VAT is decided
        vat = reference.get("vat")
        if vat:
            vat["freeagent_scheme"] = vat.get("scheme")
            scheme = self.db.get_state("freeagent:vat_scheme")
            if scheme in VAT_SCHEMES:
                vat.update(scheme=scheme, registered=scheme != "not registered", chosen=True)
        return reference

    def set_vat_scheme(self, scheme: str) -> None:
        """Correct the VAT scheme FreeAgent reports; "" goes back to FreeAgent's.
        Only Receipt Bridge uses it: FreeAgent's own setting is unchanged."""
        if scheme:
            self.db.set_state("freeagent:vat_scheme", scheme)
        else:
            self.db.delete_state("freeagent:vat_scheme")
        self._bump()

    def sync_freeagent(self) -> bool:
        return self._enqueue("freeagent-sync", self._run_freeagent_sync)

    def _run_freeagent_sync(self) -> None:
        """Refresh the company, categories, bank accounts and the chosen
        accounts' transactions. Read-only."""
        client = self._freeagent()
        if client is None or not client.connected:
            return
        self._set_activity(busy=True, kind="freeagent", label="Reading FreeAgent", started_at=_now(), log=[])
        try:
            company = client.company()
            self._check_company(f"{client.credentials.environment}:{company.get('url') or company.get('subdomain', '')}")
            accounts = client.bank_accounts()
            categories = client.categories()
            user = client.me()
            projects = self._read_projects(client)
            reference = {
                "projects": projects,
                "user_url": user.get("url"),
                "subdomain": company.get("subdomain", ""),
                "vat": vat_settings(company),
                "environment": client.credentials.environment,
                "bank_accounts": [
                    {"url": a["url"], "name": a.get("name", ""), "currency": a.get("currency", "GBP"),
                     "type": a.get("type", ""), "status": a.get("status", "active"),
                     "is_personal": bool(a.get("is_personal"))}
                    for a in accounts
                ],
                "categories": [
                    {"url": c["url"], "description": c.get("description", ""),
                     "nominal_code": c.get("nominal_code", ""), "group": c["group"]}
                    for c in categories
                ],
                "fetched_at": _now(),
            }
            self.db.set_state("freeagent:reference", json.dumps(reference))

            since = self.db.get_state("freeagent:last_sync")
            started = _now()
            fetched = 0
            for url in self._freeagent_accounts():
                if since:
                    rows = client.bank_transactions(url, updated_since=since)
                else:
                    start = (datetime.now() - timedelta(days=FREEAGENT_HISTORY_DAYS)).date().isoformat()
                    rows = client.bank_transactions(url, from_date=start)
                for row in rows:
                    row.setdefault("bank_account", url)
                fetched += self.db.save_bank_transactions(rows)
            self.db.set_state("freeagent:last_sync", started)
            self.guess_categories()
            self.auto_link()
            self.find_emails()
            self._freeagent_error = self._freeagent_problem = ""
            self._note(f"FreeAgent: {len(accounts)} bank account(s), {len(categories)} categories, "
                       f"{fetched} transaction(s) updated")
        except NotConnected as exc:
            self._freeagent_error, self._freeagent_problem = str(exc), "error"
        except (FreeAgentError, OSError, ValueError) as exc:
            log.warning("FreeAgent sync failed: %s", exc)
            self._freeagent_error, self._freeagent_problem = _freeagent_failure(exc)

    @staticmethod
    def _read_projects(client: FreeAgent) -> list[dict[str, Any]]:
        """Active projects with their client's name, for "Re-bill to client".
        A company without projects (or without access) just has none."""
        try:
            projects = client.projects()
            if not projects:
                return []
            names = {}
            for c in client.contacts():
                person = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
                names[c["url"]] = c.get("organisation_name") or person or "Client"
        except FreeAgentError as exc:
            log.info("projects not read: %s", exc)
            return []
        return [{"url": p["url"], "name": p.get("name", ""), "client": names.get(p.get("contact"), ""),
                 "currency": p.get("currency", "GBP")} for p in projects]

    def freeagent_snapshot(self) -> dict[str, Any]:
        client = self._freeagent()
        reference = self._freeagent_reference()
        chosen = set(self._freeagent_accounts())
        return {
            "has_credentials": client is not None,
            "environment": client.credentials.environment if client else "",
            "connected": bool(client and client.connected),
            "error": self._freeagent_error,
            "problem": self._freeagent_problem if self._freeagent_error else "",
            "vat": reference.get("vat"),
            "bank_accounts": [{**a, "chosen": a["url"] in chosen}
                              for a in reference.get("bank_accounts", [])
                              if a.get("status") != "hidden"],
            "categories": len(reference.get("categories", [])),
            "projects": reference.get("projects", []),
            "last_sync": self.db.get_state("freeagent:last_sync"),
            "transactions": len(self.db.bank_transactions(sorted(chosen))),
            "dry_run": self.freeagent_dry_run,
            # FreeAgent's website for this company ("View in FreeAgent"). Deep
            # links to one explanation aren't documented, so this is the home page.
            "web": (f"https://{reference['subdomain']}."
                    f"{'sandbox.' if client and client.credentials.environment == 'sandbox' else ''}freeagent.com"
                    if reference.get("subdomain") else None),
            "waiting_days": self.waiting_days,
        }

    # ---- filing (Phase 3) ----------------------------------------------------

    @property
    def freeagent_dry_run(self) -> bool:
        """On until switched off in Settings: requests are built and shown,
        never sent (PLAN.md §9)."""
        return self.db.get_state("freeagent:dry_run", "1") == "1"

    def set_freeagent_dry_run(self, on: bool) -> None:
        self.db.set_state("freeagent:dry_run", "1" if on else "0")
        self._bump()

    @staticmethod
    def _supplier_key(vendor: str | None) -> str:
        return (vendor or "").strip().lower()

    def _category_for(self, row: Any, guess: bool = True) -> str | None:
        """The receipt's own category, else the one last used for this
        supplier, else (with `guess`) the on-device suggestion."""
        if row["category"]:
            return row["category"]
        key = self._supplier_key(row["vendor"])
        remembered = self.db.get_state(f"category_for:{key}") if key else None
        if remembered or not guess:
            return remembered
        return self._category_guess(row)

    @staticmethod
    def _bank_account(row: Any) -> str | None:
        """The one bank account this receipt was paid from, if its supplier
        rule says so (Settings → Email receipts → Paid with)."""
        try:
            return (json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}).get("bank_account") or None
        except ValueError:
            return None

    def apply_paid_with(self, watcher_id: str, paid_with: str) -> int:
        """A supplier rule's "Paid with" changed: its emails still waiting in
        Files or Expenses that you haven't decided on follow it."""
        changed = 0
        for row in self.db.list_receipts(PENDING):
            if row["watcher_id"] != watcher_id or row["paid_by"] not in (None, "business", "personal"):
                continue
            extra = json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
            if extra.get("paid_by_you"):
                continue                       # you chose for this one: leave it
            extra.pop("bank_account", None)
            if paid_with.startswith("https://"):
                extra["bank_account"] = paid_with
            self.db.update_receipt(row["id"], {"paid_by": "personal" if paid_with == "personal" else
                                               ("business" if paid_with.startswith("https://") else None),
                                               "extra_json": json.dumps(extra)})
            changed += 1
        self._bump()
        return changed

    REBILL_TYPES = ("none", "cost", "markup", "price")

    def _valid_rebill(self, value: Any) -> dict[str, Any] | None:
        """{project, type, factor}: link to a client's project and re-bill it
        not at all ("none"), at cost, with a markup (factor = percent), or at
        a set price (factor = £)."""
        if not value:
            return None
        if not isinstance(value, dict):
            raise ValueError("rebill must be {project, type, factor} or null")
        projects = {p["url"] for p in self._freeagent_reference().get("projects", [])}
        if value.get("project") not in projects:
            raise ValueError("unknown project")
        kind = value.get("type") or "cost"
        if kind not in self.REBILL_TYPES:
            raise ValueError("type must be none, cost, markup or price")
        factor = None
        if kind in ("markup", "price"):
            try:
                factor = round(float(value.get("factor")), 2)
            except (TypeError, ValueError):
                factor = None
            if factor is None or factor <= 0 or (kind == "markup" and factor > 1000):
                # not saved as a mistake: kept with no factor until it's given
                factor = None
        return {"project": value["project"], "type": kind, "factor": factor}

    @staticmethod
    def _rebill(row: Any) -> dict[str, Any] | None:
        try:
            return (json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}).get("rebill") or None
        except ValueError:
            return None

    def _category_guess(self, row: Any) -> str | None:
        """The suggested category ("Guess"), if it's still one of yours."""
        try:
            found = (json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}).get("category_guess") or {}
        except ValueError:
            return None
        known = {c["url"] for c in self._freeagent_reference().get("categories", [])}
        return found.get("url") if found.get("url") in known else None

    def _category_is_guess(self, row: Any) -> bool:
        return self._category_for(row, guess=False) is None and self._category_guess(row) is not None

    # ---- category suggestions ("Guess") --------------------------------

    def guess_categories(self) -> bool:
        """Suggest categories for receipts that have none (queued)."""
        if not self._guess_candidates(limit=1):
            return False
        return self._enqueue("category-guess", self._run_guess_categories)

    def _guess_candidates(self, limit: int = CATEGORY_GUESSES) -> list[Any]:
        if not self._freeagent_reference().get("categories"):
            return []
        out = []
        for row in self.db.list_receipts(PENDING):
            extra = json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
            if "category_guess" in extra or extra.get("not_a_receipt") or self._category_for(row, guess=False):
                continue
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def _run_guess_categories(self) -> None:
        from . import category_guess

        rows = self._guess_candidates()
        if not rows:
            return
        items = []
        for row in rows:
            extra = json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
            text = "\n".join(extra.get("rows") or []) or "\n".join(
                str(v) for v in (row["subject"], row["description"], extra.get("description")) if v)
            items.append({"id": row["id"], "supplier": row["vendor"] or "", "text": text[:2000]})
        guesses = category_guess.guess(items, self._freeagent_reference(), self.config.data_dir)
        for row in rows:
            extra = json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
            # an empty guess is recorded too, so it isn't asked again
            extra["category_guess"] = guesses.get(row["id"]) or {}
            self.db.update_receipt(row["id"], {"extra_json": json.dumps(extra)})
        if guesses:
            self._bump()

    EDITABLE = {"category", "paid_by", "total", "currency", "purchased_on", "vendor", "vat_treatment",
                "native_gross", "checked", "rebill", "auto_file", "vat_choice", "vat_amount"}
    VAT_TREATMENTS = ("printed", "reverse_charge")

    def _vat_treatment(self, row: Any) -> str:
        key = self._supplier_key(row["vendor"])
        return (self.db.get_state(f"vat_treatment_for:{key}") if key else None) or "printed"

    def set_receipt_fields(self, receipt_id: int, changes: dict[str, Any]) -> None:
        """Corrections from review. A category or a supplier name is
        remembered, so the next receipt from that supplier needs nothing."""
        unknown = set(changes) - self.EDITABLE
        if unknown:
            raise ValueError(f"can't change {sorted(unknown)}")
        row = self.db.get_receipt(receipt_id)
        if row is None:
            raise ValueError("no such receipt")
        if "vat_treatment" in changes:
            # a supplier setting, not a receipt field
            treatment = changes.pop("vat_treatment") or "printed"
            if treatment not in self.VAT_TREATMENTS:
                raise ValueError("vat_treatment must be printed or reverse_charge")
            key = self._supplier_key(changes.get("vendor") or row["vendor"])
            if not key:
                raise ValueError("name the supplier first")
            self.db.set_state(f"vat_treatment_for:{key}", treatment)
            if not changes:
                self._bump()
                return
        if "paid_by" in changes and changes["paid_by"] not in ("business", "personal", None):
            raise ValueError("paid_by must be business or personal")
        if "paid_by" in changes:
            mine = json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
            mine["paid_by_you"] = True
            mine.pop("bank_account", None)      # your choice replaces the rule's account
            row_extra_override = mine
        else:
            row_extra_override = None
        if "total" in changes and changes["total"] is not None:
            total = round(float(changes["total"]), 2)
            if total <= 0:
                raise ValueError("the total must be more than zero")
            changes["total"] = total
        if "native_gross" in changes and changes["native_gross"] not in (None, ""):
            native = round(float(changes["native_gross"]), 2)
            if native <= 0:
                raise ValueError("£ charged must be more than zero")
            changes["native_gross"] = native
        elif "native_gross" in changes:
            changes["native_gross"] = None
        if "vat_choice" in changes or "vat_amount" in changes:
            # FreeAgent's VAT menu, per receipt: Auto (as printed), an amount
            # typed in, 20%, 5%, 0%, Exempt or Out of Scope
            from .filer import AMOUNT, AUTO, VAT_CHOICES

            choice = changes.pop("vat_choice", None) or (AMOUNT if "vat_amount" in changes else AUTO)
            if choice not in VAT_CHOICES:
                raise ValueError(f"VAT must be one of {', '.join(VAT_CHOICES)}")
            mine = row_extra_override if row_extra_override is not None else (
                json.loads(row["extra_json"] or "{}") if row["extra_json"] else {})
            if "vat_amount" in changes:
                raw = changes.pop("vat_amount")
                amount = None if raw in (None, "") else round(float(str(raw).replace("£", "").strip()), 2)
                if amount is not None and amount < 0:
                    raise ValueError("VAT can't be negative")
                total = changes.get("total") or row["total"]
                if amount and total and amount > round(float(total) * 20 / 120, 2) + 0.01:
                    raise ValueError(f"£{amount:.2f} is more than 20% VAT on £{float(total):.2f}")
                mine["vat_amount"] = amount
            mine["vat_choice"] = choice
            if choice != AUTO:
                mine["flags"] = [f for f in mine.get("flags", []) if "VAT" not in f]   # your call settles it
            row_extra_override = mine
        if changes.get("currency"):
            currency = str(changes["currency"]).strip().upper()
            if not (len(currency) == 3 and currency.isalpha()):
                raise ValueError("currency must be a three-letter code, like GBP or EUR")
            changes["currency"] = currency
        if changes.get("purchased_on"):
            try:
                changes["purchased_on"] = datetime.strptime(str(changes["purchased_on"]), "%Y-%m-%d").date().isoformat()
            except ValueError as exc:
                raise ValueError("date must be YYYY-MM-DD") from exc
        if changes.get("vendor") is not None:
            changes["vendor"] = str(changes["vendor"]).strip()[:80] or None
        if "category" in changes and changes["category"]:
            known = {c["url"] for c in self._freeagent_reference().get("categories", [])}
            if changes["category"] not in known:
                raise ValueError("unknown category")
        # A correction settles the doubts about that field: clear its flags.
        extra = row_extra_override if row_extra_override is not None else (
            json.loads(row["extra_json"] or "{}") if row["extra_json"] else {})
        if row_extra_override is not None:
            changes["extra_json"] = extra
        if "auto_file" in changes:
            # "Link {supplier} automatically from now on": your approval for
            # this supplier. Nothing else ever turns it on.
            key = self._supplier_key(changes.get("vendor") or row["vendor"])
            if not key:
                raise ValueError("name the supplier first")
            if changes.pop("auto_file"):
                self.db.set_state(f"auto_link:{key}", "1")
            else:
                self.db.delete_state(f"auto_link:{key}")
            self.auto_link()
            if not changes:
                self._bump()
                return
        if "rebill" in changes:
            extra["rebill"] = self._valid_rebill(changes.pop("rebill"))
            changes["extra_json"] = extra
        checked = bool(changes.pop("checked", False))
        if checked or changes.get("total") or changes.get("vendor"):
            # "Looks right" in Files, or details typed in: it is a receipt
            if extra.pop("not_a_receipt", None) is not None:
                changes["extra_json"] = extra
        if checked and extra.get("flags"):
            # every doubt is settled, except a blank that's still blank
            still = {"total": row["total"] is None and changes.get("total") is None,
                     "currency": not (row["currency"] or changes.get("currency")),
                     "paid_by": "paid_by" not in changes and row["paid_by"] is None}
            extra["flags"] = [f for f in extra["flags"]
                              if any(still[k] and any(w in f for w in FLAG_WORDS[k]) for k in still)]
            changes["extra_json"] = extra
        if extra.get("flags") and not checked:
            settled = [w for field, words in FLAG_WORDS.items() if field in changes for w in words]
            extra["flags"] = [f for f in extra["flags"] if not any(w in f for w in settled)]
            if "total" in changes:
                changes["total_status"] = "confirmed by you"
            changes["extra_json"] = extra
        if ("vendor" in changes or checked) and extra.get("supplier_source") in ("model", "first line"):
            extra["supplier_source"] = "you"            # a name you've checked is no longer a guess
            changes["extra_json"] = extra
        if changes:
            self.db.update_receipt(receipt_id, changes)
        vendor = changes.get("vendor") or row["vendor"]
        if changes.get("category") and self._supplier_key(vendor):
            self.db.set_state(f"category_for:{self._supplier_key(vendor)}", changes["category"])
        if changes.get("vendor") and row["vat_number"]:
            self.db.set_state(f"supplier_by_vat:{row['vat_number']}", changes["vendor"])
        self._bump()

    # ---- settings ----------------------------------------------------------

    @property
    def waiting_days(self) -> int:
        """How long a receipt may wait for its bank payment before it's
        flagged (PLAN.md §8.2, §12). Bank feeds and card statements differ."""
        try:
            return max(1, int(self.db.get_state("pref:waiting_days", "10") or 10))
        except ValueError:
            return 10

    def set_waiting_days(self, days: int) -> None:
        if not 1 <= int(days) <= 90:
            raise ValueError("waiting days must be between 1 and 90")
        self.db.set_state("pref:waiting_days", str(int(days)))
        self._bump()

    @property
    def archive_delete_days(self) -> int:
        """Delete archived receipts this many days after they were archived;
        0 keeps them for ever. 30 unless changed in Settings."""
        try:
            return max(0, int(self.db.get_state("pref:archive_delete_days", "30") or 0))
        except ValueError:
            return 0

    def set_archive_delete_days(self, days: int) -> None:
        if not 0 <= int(days) <= 3650:
            raise ValueError("days must be between 0 (never) and 3650")
        self.db.set_state("pref:archive_delete_days", str(int(days)))
        self._bump()
        self.purge_archived()

    def delete_archived(self, receipt_ids: list[int] | None = None) -> int:
        """Delete ignored and unreadable receipts for good: every one when no
        ids are given. Returns how many went."""
        archived = set(self.db.archived_ids())
        targets = archived if receipt_ids is None else archived & set(receipt_ids)
        for receipt_id in targets:
            self._delete_receipt(receipt_id)
        if targets:
            log.info("deleted %d archived receipt(s)", len(targets))
            self._bump()
        return len(targets)

    def purge_archived(self) -> int:
        """Delete receipts archived longer ago than the Settings choice."""
        days = self.archive_delete_days
        if not days:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        ids = self.db.archived_ids(before=cutoff)
        return self.delete_archived(ids) if ids else 0

    def _delete_receipt(self, receipt_id: int) -> None:
        """Remove a receipt's files and hide it. The row stays, marked
        deleted, so a later scan knows the email was already seen."""
        row = self.db.get_receipt(receipt_id)
        if row is None:
            return
        # Only files the app made or moved: never anything outside its own
        # data or the photo archive, nor a file another receipt still uses.
        roots = [self.config.data_dir.resolve(), self.photo_archive.resolve()]
        for key in ("pdf_path", "original_path"):
            if not row[key]:
                continue
            path = Path(row[key]).resolve()
            if not any(root == path.parent or root in path.parents for root in roots):
                continue
            if self.db.path_in_use(row[key], receipt_id):
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.info("couldn't delete %s: %s", path, exc)
        self.db.update_receipt(receipt_id, {"status": DELETED, "pdf_path": None, "original_path": None,
                                            "extra_json": None, "error": None})

    def _auto_link_on(self, row: Any) -> bool:
        """You ticked "Link {supplier} automatically from now on"."""
        key = self._supplier_key(row["vendor"])
        return bool(key) and self.db.get_state(f"auto_link:{key}") == "1"

    def auto_link_candidates(self) -> list[int]:
        """Receipts from suppliers you approved that can link themselves: an
        exact payment (amount, date window, the supplier's name on the
        statement), nothing to check, a category that isn't a guess, paid
        from the business, dry run off."""
        if self.freeagent_dry_run or not self._freeagent_connected():
            return []
        reference = self._freeagent_reference()
        names = {c["url"]: c["description"] for c in reference.get("categories", [])}
        registered = bool(reference.get("vat", {}).get("registered"))
        rows = [r for r in self.db.list_receipts(PENDING) if self._auto_link_on(r) and r["paid_by"] != "personal"]
        if not rows:
            return []
        matches = self._matches(self.db.list_receipts(PENDING))
        ready = []
        for row in rows:
            m = matches.get(row["id"])
            if m is None or m.status != "matched" or not m.exact or not m.alias_hit or self._category_is_guess(row):
                continue
            r = review(row, m, self._context(row, names, registered, m), overdue=False)
            if r["group"] == "ready" and r["stage"] == "link":
                ready.append(row["id"])
        return ready

    def auto_link(self) -> bool:
        if not self.auto_link_candidates():
            return False
        return self._enqueue("auto-link", self._run_auto_link)

    def _run_auto_link(self) -> None:
        ids = self.auto_link_candidates()          # recheck on the worker: things may have changed
        if ids:
            self._run_file(ids)
            if self._outcome and self._outcome.found:
                self.notify_about("filed", f"{self._outcome.message}: suppliers you set to link automatically.")

    @staticmethod
    def _freeagent_category(match: Any) -> str | None:
        """The category of FreeAgent's own explanation, when the matched
        payment is already explained (a bank rule, an accepted guess)."""
        t = match.transaction if match is not None else None
        if t and t.get("explanation_url") and not t.get("unexplained_amount"):
            return t.get("explanation_category") or None
        return None

    def _shown_category(self, row: Any, match: Any) -> str | None:
        """For a payment FreeAgent already explained, its category unless
        you chose another; otherwise the usual (yours, remembered, guessed)."""
        return row["category"] or self._freeagent_category(match) or self._category_for(row)

    def _context(self, row: Any, names: dict[str, str], registered: bool, match: Any = None) -> Context:
        category = self._shown_category(row, match)
        return Context(vat_registered=registered, vat_treatment=self._vat_treatment(row),
                       category_name=names.get(category or "", ""), waiting_days=self.waiting_days,
                       today=datetime.now().date())

    def _overdue(self, row: Any, match: Any) -> bool:
        if match is None or match.status != "waiting" or not row["purchased_on"]:
            return False
        try:
            waited = (datetime.now().date() - datetime.strptime(row["purchased_on"][:10], "%Y-%m-%d").date()).days
        except ValueError:
            return False
        return waited > self.waiting_days

    def file(self, receipt_ids: list[int]) -> bool:
        ids = sorted(set(receipt_ids))
        return self._enqueue(f"file:{','.join(map(str, ids))}", lambda: self._run_file(ids))

    def _run_file(self, ids: list[int], only_single_part: bool = False, live: bool = False) -> None:
        dry = self.freeagent_dry_run and not live       # live: "Approve" is never a dry run
        self._set_activity(busy=True, kind="file", label="Dry run" if dry else "Filing to FreeAgent",
                           started_at=_now(), log=[])
        client = self._freeagent()
        if client is None or not client.connected:
            with self._lock:               # the File all dialog waits for a result
                self._file_results = {"at": _now(), "dry_run": dry, "results": [],
                                      "message": "FreeAgent isn't connected: reconnect in Settings, then try again."}
            self._finish("file", False, "Connect FreeAgent first")
            return
        reference = self._freeagent_reference()
        registered = bool(reference.get("vat", {}).get("registered"))
        pending = self.db.list_receipts(PENDING)
        matches = self._matches(pending)          # all pending, so pairing is the same as shown
        done = failed = 0
        results: list[dict[str, Any]] = []
        for receipt_id in ids:
            row = self.db.get_receipt(receipt_id)
            if row is None or row["status"] != PENDING:
                continue
            match = matches.get(receipt_id)
            transaction = match.transaction if match and match.status == "matched" else None
            # FreeAgent's guess on the payment: filed as a new explanation in
            # its place, so it's approved, not just attached to (_replace_guess)
            guess = transaction is not None and (self._freeagent_explanation(transaction) or {}).get("guess")
            plan_tx = ({**transaction, "explanation_url": None, "unexplained_amount": transaction["amount"]}
                       if guess else transaction)
            plan = plan_for(row, transaction=plan_tx, category_url=self._shown_category(row, match),
                            user_url=reference.get("user_url"), vat_registered=registered,
                            vat_treatment=self._vat_treatment(row),
                            allow_difference=bool(match and match.pinned and not match.exact),
                            rebill=self._rebill(row))
            if only_single_part and len(plan.parts) > 1:
                self._note(f"{row['vendor']}: two VAT rates, left for you to file")
                continue
            try:
                if guess and not dry and not plan.problems:
                    line = self._replace_guess(
                        client, transaction,
                        lambda _guess: file_receipt(client, self.db, receipt_id, plan, dry_run=False),
                        with_receipt=True).replace("Filed", "Approved and linked", 1)
                else:
                    line = file_receipt(client, self.db, receipt_id, plan, dry_run=dry)
                self._note(line)
                done += 1
                results.append({"id": receipt_id, "vendor": row["vendor"], "total": row["total"],
                                "ok": True, "note": line})
                if plan.body.get("category") and row["vendor"]:
                    self.db.set_state(f"category_for:{self._supplier_key(row['vendor'])}", plan.body["category"])
            except (FilingError, FreeAgentError, OSError) as exc:
                failed += 1
                self._note(f"{row['vendor'] or 'Receipt'}: {exc}")
                note = str(exc)
                if "already (partly) explained" in note:
                    note = ("This payment was explained in FreeAgent since the last check, so it was "
                            "left alone. The receipt is back in Match.")
                results.append({"id": receipt_id, "vendor": row["vendor"], "total": row["total"],
                                "ok": False, "note": note})
                previous = json.loads(self.db.get_receipt(receipt_id)["freeagent_json"] or "{}")
                if previous.get("state") not in ("filing", "explained"):   # never hide an interrupted filing
                    self.db.update_receipt(receipt_id, {"freeagent_json": json.dumps(
                        {"state": "problem", "message": str(exc), "at": _now()})})
        if done and not dry:
            self.sync_freeagent()                 # the payments just explained
        with self._lock:
            self._file_results = {"at": _now(), "dry_run": dry, "results": results}
        verb = "Dry run for" if dry else "Filed"
        message = f"{verb} {done} receipt{'s' if done != 1 else ''}"
        if failed:
            message += f" · {failed} need attention"
        self._finish("file", not failed, message, found=done)

    def unfile_many(self, receipt_ids: list[int]) -> bool:
        """Undo a whole File all ("Undo all 4")."""
        ids = sorted(set(receipt_ids))
        return self._enqueue(f"unfile:{','.join(map(str, ids))}", lambda: [self._run_unfile(i) for i in ids])

    def set_payment(self, receipt_id: int, transaction_url: str | None) -> None:
        """Pin the bank payment for a receipt ("This one", "Use this", Change
        payment), or None to go back to automatic matching."""
        row = self.db.get_receipt(receipt_id)
        if row is None:
            raise ValueError("no such receipt")
        t = None
        if transaction_url:
            known = {t["url"]: dict(t) for t in self.db.bank_transactions(self._freeagent_accounts())}
            t = known.get(transaction_url)
            if t is None:
                raise ValueError("that payment isn't in the bank accounts being matched")
            if not needs_receipt(t):
                raise ValueError("that payment already has its receipt in FreeAgent")
        changes: dict[str, Any] = {"transaction_url": transaction_url or None}
        # A receipt with no date can't be matched, so choosing its payment
        # did nothing ("Use this" on an undated photo). It takes the payment's
        # date instead, which is the date filed against the bank, and says so.
        # Choosing another payment moves the date with it; going back to
        # automatic matching takes it away. A date you typed is never touched.
        extra = json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
        before = json.dumps(extra, sort_keys=True)
        flags = list(extra.get("flags") or [])
        if transaction_url and transaction_url in (extra.get("not_payments") or []):
            extra["not_payments"].remove(transaction_url)     # you chose it after all
        borrowed = DATE_FROM_PAYMENT in flags
        if t is not None and (borrowed or not row["purchased_on"]):
            if not borrowed:
                extra["no_date_flags"] = [f for f in flags if "No date" in f]
                flags = [f for f in flags if "No date" not in f] + [DATE_FROM_PAYMENT]
            changes["purchased_on"] = t["dated_on"][:10]
        elif t is None and borrowed:
            flags = [f for f in flags if f != DATE_FROM_PAYMENT] + extra.pop("no_date_flags", [])
            changes["purchased_on"] = None
        # A supplier name the Mac guessed is confirmed when the payment you
        # chose names it on the statement ("SAINSBURY'S // CARD_PURCHASE").
        # Another payment, or none, puts the doubt back, unless you've since
        # named the supplier yourself.
        token = supplier_token(row["vendor"])
        named = t is not None and bool(token) and token in (t["description"] or "").upper()
        guessed = [f for f in flags if f.startswith("Supplier name guessed")]
        stashed = extra.get("supplier_flags") or {}
        if named and guessed:
            extra["supplier_flags"] = {"vendor": row["vendor"], "flags": guessed}
            flags = [f for f in flags if f not in guessed]
        elif not named and stashed:
            extra.pop("supplier_flags")
            if stashed.get("vendor") == row["vendor"]:
                flags += stashed.get("flags", [])
        if flags != list(extra.get("flags") or []):
            extra["flags"] = flags
        if json.dumps(extra, sort_keys=True) != before:
            changes["extra_json"] = extra
        self.db.update_receipt(receipt_id, changes)
        self._bump()

    def remove_file_from_payment(self, receipt_id: int, transaction_url: str) -> None:
        """"Remove file": this file isn't that payment's receipt. Whether you
        chose it or it was suggested, it's never suggested there again."""
        row = self.db.get_receipt(receipt_id)
        if row is None:
            raise ValueError("no such receipt")
        if row["transaction_url"] == transaction_url:
            self.set_payment(receipt_id, None)          # also gives back a borrowed date
            row = self.db.get_receipt(receipt_id)
        extra = json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
        refused = extra.setdefault("not_payments", [])
        if transaction_url not in refused:
            refused.append(transaction_url)
        self.db.update_receipt(receipt_id, {"extra_json": extra})
        self._bump()

    def update_claim(self, receipt_id: int) -> bool:
        """"Update FreeAgent" on a claimed expense: send its edited details."""
        return self._enqueue(f"file:update:{receipt_id}", lambda: self._run_update_claim(receipt_id))

    def _run_update_claim(self, receipt_id: int) -> None:
        from .filer import update_claim
        dry = self.freeagent_dry_run
        self._set_activity(busy=True, kind="file", label="Dry run" if dry else "Updating FreeAgent",
                           started_at=_now(), log=[])
        client = self._freeagent()
        if client is None or not client.connected:
            self._finish("file", False, "Connect FreeAgent first")
            return
        row = self.db.get_receipt(receipt_id)
        if row is None:
            self._finish("file", False, "That receipt has gone")
            return
        reference = self._freeagent_reference()
        plan = plan_for(row, transaction=None, category_url=self._shown_category(row, None),
                        user_url=reference.get("user_url"),
                        vat_registered=bool(reference.get("vat", {}).get("registered")),
                        vat_treatment=self._vat_treatment(row), rebill=self._rebill(row))
        try:
            self._finish("file", True, update_claim(client, self.db, receipt_id, plan, dry_run=dry))
        except (FilingError, FreeAgentError) as exc:
            self._finish("file", False, f"Not changed: {exc}")

    def unfile(self, receipt_id: int) -> bool:
        return self._enqueue(f"unfile:{receipt_id}", lambda: self._run_unfile(receipt_id))

    def _run_unfile(self, receipt_id: int) -> None:
        self._set_activity(busy=True, kind="file", label="Unfiling", started_at=_now(), log=[])
        client = self._freeagent()
        if client is None or not client.connected:
            self._finish("file", False, "Connect FreeAgent first")
            return
        try:
            self._finish("file", True, unfile(client, self.db, receipt_id))
            self.sync_freeagent()
        except (FilingError, FreeAgentError) as exc:
            self._finish("file", False, f"Couldn't unfile: {exc}")

    def _matches(self, rows: list[Any]) -> dict[int, Any]:
        """Read-only proposals: which bank transaction each receipt fits."""
        chosen = self._freeagent_accounts()
        if not chosen:
            return {}
        accounts = {a["url"]: a for a in self._freeagent_reference().get("bank_accounts", [])}
        transactions = [
            {**dict(t), "currency": accounts.get(t["bank_account"], {}).get("currency", "GBP")}
            for t in self.db.bank_transactions(chosen)
        ]
        currencies = {accounts[u].get("currency", "GBP") for u in chosen if u in accounts}
        receipts = [
            {"id": r["id"], "date": r["purchased_on"], "total": r["total"], "currency": r["currency"],
             "vendor": r["vendor"], "paid_by": r["paid_by"], "pinned": r["transaction_url"],
             "undated": is_undated(r), "account": self._bank_account(r),
             "refused": (json.loads(r["extra_json"] or "{}") if r["extra_json"] else {}).get("not_payments")}
            for r in rows
        ]
        return match_receipts(receipts, transactions, currencies)

    # ---- retrying one receipt -------------------------------------------

    def retry(self, receipt_id: int, quiet: bool = False) -> bool:
        record = self.db.get_receipt(receipt_id)
        if record is not None and record["source"] == "photo":
            return self._enqueue(f"retry:{receipt_id}", lambda: self._reread_photo(receipt_id))
        return self._enqueue(
            f"retry:{receipt_id}", lambda: self._run_retry(receipt_id, quiet)
        )

    def reread_partly_read_emails(self) -> int:
        """Emails once set aside as "couldn't read" because the supplier rule
        missed a field: read them again, now staged like a photo with what was
        found and the rest flagged (app/pipeline.py)."""
        ids = [r["id"] for r in self.db.list_receipts(FAILED)
               if r["gmail_message_id"] and "could not find required field" in (r["error"] or "")]
        for receipt_id in ids:
            self.retry(receipt_id, quiet=True)
        return len(ids)

    def _reread_photo(self, receipt_id: int) -> None:
        self._set_activity(busy=True, kind="retry", label="Reading the photo again", started_at=_now())
        ok = reread(self.config, self.db, receipt_id)
        self._finish("retry", ok, "Photo read again" if ok else "The photo still couldn't be read")

    def _run_retry(self, receipt_id: int, quiet: bool = False) -> None:
        """Try again for one receipt.

        For an email-copy receipt: fetch the supplier's own document. For one
        that failed to parse: run it through the whole pipeline again — the
        supplier rule may have been fixed since. Without this, a failure was
        permanent, because the message id was already recorded.

        `quiet` is for automatic retries: a success is announced, a failure
        is not — the receipt simply stays as it was.
        """
        from .browser import BrowserPool
        from .pipeline import ScanReport, _safe_stem, process_email, resolve_pdf

        record = self.db.get_receipt(receipt_id)
        if record is None:
            return
        vendor = record["vendor"] or record["watcher_id"]
        self._set_activity(
            busy=True, kind="retry", label=f"Fetching {vendor} receipt",
            started_at=_now(), log=[],
        )
        self.db.note_fetch_attempt(receipt_id, _now())

        watchers, _ = load_watchers_safe(self.config.watchers_dir)
        watcher = next((w for w in watchers if w.id == record["watcher_id"]), None)
        account = self.accounts.get(record["account"]) if record["account"] else None
        account = account or next(iter(self.accounts.list()), None)
        if watcher is None or account is None:
            if not quiet:
                self._finish("retry", False, "That supplier or account is no longer set up.")
            return

        def fail(message: str) -> None:
            log.info("retry of receipt %s: %s", receipt_id, message)
            if not quiet:
                self._finish("retry", False, message)

        try:
            client = account.client(self.config.credentials_file, self.config.scopes)
            message = client.fetch(record["gmail_message_id"], record["gmail_thread_id"] or "")
        except Exception as exc:
            return fail(f"Couldn't read that email from Gmail: {exc}")

        pool = BrowserPool(
            headless=self.config.headless,
            block_trackers=bool(self.config.pdf.get("block_trackers", True)),
        )
        try:
            if record["status"] == FAILED:
                self.db.delete_receipt(receipt_id)
                report = ScanReport()
                new_id = process_email(
                    self.config, self.db, watcher, message, pool, report, record["account"] or ""
                )
                if new_id:
                    self._finish("retry", True, f"{vendor} receipt read successfully.")
                else:
                    fail(f"Still couldn't read it. {' '.join(report.notes[-1:])}")
                return

            values = watcher.extract(message)
            out = self.config.pdf_dir / f"{_safe_stem(message.message_id)}.pdf"
            path, source, _ = resolve_pdf(self.config, pool, watcher, message, values, out)
        except Exception as exc:
            return fail(f"Couldn't fetch it: {exc}")
        finally:
            pool.close()

        self.db.update_pdf(receipt_id, str(path), source)
        if source == "rendered_email":
            fail(f"{vendor} still isn't offering the receipt — kept the email copy.")
        else:
            self._finish("retry", True, f"Got {vendor}'s own receipt.")
            if quiet:
                self.notify_about("filed", f"Got {vendor}'s own receipt for one that only had an email copy.")

    def _queue_automatic_retries(self) -> int:
        """Give missed supplier documents another chance, a few times.

        Suppliers' sites have bad hours — Trainline rate-limits, tokens are
        briefly unavailable — so a receipt that fell back to the email is
        retried automatically, at most every 12 hours and three times in all,
        rather than staying an email copy until someone notices.
        """
        cutoff = (datetime.now(timezone.utc) - AUTO_RETRY_GAP).isoformat(timespec="seconds")
        ids = self.db.retry_candidates(
            self._suppliers_with_better_documents(), AUTO_RETRY_LIMIT, cutoff
        )
        for receipt_id in ids:
            self.retry(receipt_id, quiet=True)
        return len(ids)

    # ---- accounts -------------------------------------------------------

    def check_accounts(self) -> bool:
        return self._enqueue("health", self._run_health)

    def _run_health(self) -> None:
        self._set_activity(busy=True, kind="health", label="Checking Gmail", started_at=_now())
        for account in self.accounts.list():
            state, detail = account.check(self.config.credentials_file, self.config.scopes)
            with self._lock:
                previous = self._health.get(account.email, Health()).state
                self._health[account.email] = Health(state, detail, _now())
            # Say so once, when it changes — otherwise a lapsed sign-in goes
            # unnoticed until someone opens the app and wonders why nothing
            # new has arrived.
            if previous == "ok" and state in ("expired", "signed_out"):
                self.notify_about(
                    "problems",
                    f"Gmail access for {account.email} has lapsed. Open Receipt Bridge to reconnect.",
                )
        # Forget accounts that have since been removed.
        known = {a.email for a in self.accounts.list()}
        with self._lock:
            for email in list(self._health):
                if email not in known:
                    del self._health[email]

    def connect_account(self, scan_from: date | None = None) -> bool:
        """Sign in to Google in the user's real browser.

        `scan_from`: how far back to look for receipts in this mailbox (asked
        before sign-in); its first scan starts there. Runs on its own thread,
        not the worker: sign-in waits on a person, for up to five minutes, and
        nothing else should queue behind that.
        """
        with self._lock:
            if self._connecting:
                return False
            self._connecting = True
        self._bump()

        def run() -> None:
            try:
                account = self.accounts.add_interactive()
            except SignInCancelled:
                self._finish("connect", False, "Gmail sign-in cancelled.")
            except Exception as exc:
                self._finish("connect", False, f"Sign-in didn't complete: {exc}")
            else:
                if scan_from:
                    self.db.set_state(f"scan_from:{account.email}", scan_from.isoformat())
                self._finish("connect", True, f"Connected {account.email}")
                self.check_accounts()
                self.scan("connected")          # its receipts, from the date chosen
            finally:
                with self._lock:
                    self._connecting = False
                self._bump()

        threading.Thread(target=run, name="receipt-connect", daemon=True).start()
        return True

    def cancel_connect(self) -> bool:
        """Give up on a Gmail sign-in still waiting on the browser."""
        return self.accounts.cancel_interactive()

    def disconnect_account(self, email: str) -> bool:
        removed = self.accounts.remove(email)
        with self._lock:
            self._health.pop(email, None)
            for key in [k for k in self._email_cache if k[0] == email]:
                del self._email_cache[key]
        self._bump()
        return removed

    # ---- receipts -------------------------------------------------------

    def set_status(self, receipt_ids: list[int], status: str) -> None:
        if status not in (PENDING, IGNORED):
            raise ValueError(f"cannot set status to {status!r} directly")
        for receipt_id in receipt_ids:
            self.db.set_status(receipt_id, status)
        self._bump()

    def export(self, receipt_ids: list[int]) -> ExportResult:
        result = export_receipts(self.config, self.db, receipt_ids)
        self._bump()
        return result

    def receipts(self, status: str) -> list[dict[str, Any]]:
        if status not in (PENDING, EXPORTED, IGNORED, FAILED):
            raise ValueError(f"unknown status {status!r}")
        better = self._suppliers_with_better_documents()
        rows = self.db.list_receipts(status)
        if status == EXPORTED:                 # "Filed": exported folders and filed into FreeAgent
            rows = sorted(rows + self.db.list_receipts(FILED),
                          key=lambda r: (r["purchased_on"] or r["email_date"] or "", r["id"]), reverse=True)
        reference = self._freeagent_reference()
        names = {c["url"]: c["description"] for c in reference.get("categories", [])}
        registered = bool(reference.get("vat", {}).get("registered"))
        matches = self._matches(rows) if status == PENDING else {}
        today = datetime.now().date().isoformat()
        out = []
        for row in rows:
            item = _receipt_json(row, better)
            m = matches.get(row["id"])
            fa_category = self._freeagent_category(m)
            category = self._shown_category(row, m)
            item["category"] = category or ""
            item["category_name"] = names.get(category or "", "")
            # FreeAgent's own explanation already has this category
            item["category_from_freeagent"] = bool(fa_category and category == fa_category)
            item["freeagent_category"] = fa_category
            item["vat_treatment"] = self._vat_treatment(row)
            extra_vat = json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
            item["vat_choice"] = extra_vat.get("vat_choice") or "auto"
            item["vat_amount"] = extra_vat.get("vat_amount")
            try:
                item["filing"] = json.loads(row["freeagent_json"] or "null")
            except ValueError:
                item["filing"] = None
            overdue = self._overdue(row, m)
            item["native_gross"] = row["native_gross"]
            # when the photo was taken: "Use photo date" for a receipt with no date
            item["photo_taken"] = (row["photo_taken"] or "")[:10] or None
            item["rebill"] = self._rebill(row)
            item["auto_file"] = self._auto_link_on(row)
            item["bank_account"] = self._bank_account(row)
            pages = self._page_count(row)
            item["page_count"] = pages
            item["highlights"] = self._highlights(row, all_pages=bool(pages)) if (item.get("is_image") or pages) else []
            item["tidy"] = self._tidy_json(row)
            item["pinned_payment"] = row["transaction_url"]
            item["filed_today"] = bool(row["filed_at"] and self._local_day(row["filed_at"]) == today)
            if status == PENDING or row["status"] == FILED:
                item.update(review(row, m, self._context(row, names, registered, m), overdue=overdue))
                item["ai_guess"]["category"] = self._category_is_guess(row) and not fa_category
            item["match"] = None if m is None else {
                "status": m.status,
                "reason": m.reason,
                # waiting longer than the setting: no payment is coming by itself
                # (wrong amount, a different card, a refund): needs attention
                "overdue": overdue,
                "candidates": m.candidates,
                "transaction": None if m.transaction is None else {
                    "date": m.transaction["dated_on"],
                    "amount": m.transaction["amount"],
                    # statement imports read "ANTHROPIC//OTHER/": show the name part
                    "description": (m.transaction.get("description") or "").split("//")[0],
                    # already explained in FreeAgent: filing only attaches the receipt
                    # (a guess is replaced instead: service._replace_guess)
                    "explained": explained_for_good(m.transaction),
                },
            }
            out.append(item)
        return out

    @staticmethod
    def _page_count(row: Any) -> int:
        """Pages of a PDF receipt rendered as images (for highlights), or 0."""
        try:
            return int((json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}).get("pages") or 0)
        except (ValueError, TypeError):
            return 0

    def page_image(self, receipt_id: int, page: int) -> Path | None:
        path = self.config.data_dir / "pages" / str(int(receipt_id)) / f"page-{int(page)}.jpg"
        return path if path.is_file() else None

    @staticmethod
    def _highlights(row: Any, all_pages: bool = False) -> list[dict[str, Any]]:
        """Boxes on the photo around what was read, from its current values."""
        from .highlights import locate
        try:
            layout = (json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}).get("layout") or []
        except ValueError:
            return []
        if not layout:
            return []
        return [h for h in locate(layout, supplier=row["vendor"], total=row["total"], when=row["purchased_on"],
                                  vat_number=row["vat_number"], currency=row["currency"]) if all_pages or h["page"] == 0]

    def backfill_layouts(self) -> bool:
        """Photos read before positions were recorded: read their layout once
        (OCR only; their details stay exactly as they are)."""
        if not self._layout_candidates():
            return False
        return self._enqueue("layouts", self._run_backfill_layouts)

    def _layout_candidates(self) -> list[Any]:
        """Pending receipts with no recorded layout: photos read before
        positions were kept, and PDFs (emails, dropped files), whose pages
        are also rendered so the highlights can be drawn on them."""
        out = []
        for row in self.db.list_receipts(PENDING):
            extra = json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
            document = Path(row["pdf_path"]) if row["pdf_path"] else None
            if document and document.suffix.lower() == ".pdf":
                if "pages" not in extra and document.exists():
                    out.append(row)
            elif row["source"] == "photo" and row["original_path"]:
                if "layout" not in extra and Path(row["original_path"]).exists():
                    out.append(row)
        return out

    def backfill_tidy(self) -> bool:
        """Photos waiting to be filed that arrived before tidying existed:
        offer them a tidied copy. Only the picture (and where highlights
        sit) can change; supplier, total, date and the rest stay exactly as
        they are, including anything corrected by hand."""
        if not self._tidy_candidates():
            return False
        return self._enqueue("tidy", self._run_backfill_tidy)

    def _tidy_candidates(self) -> list[Any]:
        out = []
        for row in self.db.list_receipts(PENDING):
            if row["source"] != "photo" or not row["original_path"] or not row["pdf_path"]:
                continue
            extra = json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
            if "tidy" not in extra and Path(row["original_path"]).exists() \
                    and Path(row["pdf_path"]).suffix.lower() in IMAGE_TYPES:
                out.append(row)
        return out

    def _run_backfill_tidy(self) -> None:
        from .photo_inbox import tidy
        from .receipt_reader import ReaderError, read_file
        for row in self._tidy_candidates()[:50]:
            plain = Path(row["pdf_path"])
            try:
                reading = read_file(Path(row["original_path"]), self.config.data_dir,
                                    jpeg_out=plain, clean_prefix=plain.with_suffix(""))
            except ReaderError as exc:
                log.info("tidy for receipt %s: %s", row["id"], exc)
                reading, record = None, None
            else:
                reading, record = tidy(reading)
            fresh = self.db.get_receipt(row["id"])          # it may have changed meanwhile
            if fresh is None or fresh["status"] != PENDING:
                continue
            extra = json.loads(fresh["extra_json"] or "{}") if fresh["extra_json"] else {}
            # recorded even when nothing came of it, so it isn't tried again
            extra["tidy"] = record or {"used": False, "notes": ["Couldn't be tidied."]}
            changes: dict[str, Any] = {"extra_json": json.dumps(extra)}
            if record and record["used"]:
                extra["layout"] = record["tidied_layout"]
                changes = {"extra_json": json.dumps(extra), "pdf_path": record["tidied_path"]}
            self.db.update_receipt(row["id"], changes)
        self._bump()

    def _run_backfill_layouts(self) -> None:
        from .receipt_reader import ReaderError, read_file
        for row in self._layout_candidates()[:50]:
            document = Path(row["pdf_path"]) if row["pdf_path"] else None
            pdf = bool(document and document.suffix.lower() == ".pdf")
            pages_dir = self.config.data_dir / "pages" / str(row["id"])
            try:
                reading = (read_file(document, self.config.data_dir, pages_out=pages_dir) if pdf
                           else read_file(Path(row["original_path"]), self.config.data_dir))
            except ReaderError as exc:
                log.info("layout for receipt %s: %s", row["id"], exc)
                reading = None
            fresh = self.db.get_receipt(row["id"])          # it may have changed meanwhile
            extra = json.loads(fresh["extra_json"] or "{}") if fresh["extra_json"] else {}
            extra["layout"] = reading.layout if reading else []
            if pdf:
                extra["pages"] = len(reading.page_images) if reading else 0
            self.db.update_receipt(row["id"], {"extra_json": json.dumps(extra)})
        self._bump()

    @staticmethod
    def _local_day(stamp: str) -> str:
        try:
            return datetime.fromisoformat(stamp).astimezone().date().isoformat()
        except ValueError:
            return ""

    # ---- Statement (PLAN.md §11): one month of a bank account ---------------

    def statement(self, account: str | None, month: str | None, limit: int | None = None) -> dict[str, Any]:
        """Every payment in a month of the cached statement, and whether it
        has a receipt: filed, in Match, missing, or no receipt needed.
        `month="all"`: every month, newest first, the latest `limit` of them
        (all when None)."""
        chosen = self._freeagent_accounts()
        accounts = {a["url"]: a for a in self._freeagent_reference().get("bank_accounts", [])}
        account = account or (chosen[0] if chosen else None)
        if account is None:
            return {"account": None, "rows": [], "summary": {}}
        month = month or datetime.now().strftime("%Y-%m")
        # Outgoing payments only: money in (a client paying an invoice) never
        # needs a receipt, and listing it buried the payments that do.
        every = month == "all"
        transactions = [dict(t) for t in self.db.bank_transactions([account])
                        if (every or t["dated_on"].startswith(month)) and float(t["amount"]) < 0]
        total_payments = len(transactions)
        if every:
            transactions.sort(key=lambda t: (t["dated_on"], t["url"]), reverse=True)
            transactions = transactions[:limit] if limit else transactions
        else:
            transactions.sort(key=lambda t: (t["dated_on"], t["url"]))

        pending = self.db.list_receipts(PENDING)
        matches = self._matches(pending)
        in_match = {}
        for row in pending:
            m = matches.get(row["id"])
            if m is not None and m.transaction is not None:
                in_match[m.transaction["url"]] = (row, m)
        filed_by_app = {}
        for row in self.db.list_receipts(FILED):
            state = json.loads(row["freeagent_json"] or "{}") if row["freeagent_json"] else {}
            if state.get("transaction"):
                filed_by_app[state["transaction"]] = row

        rows, counts = [], {"filed": 0, "in_match": 0, "missing": 0, "not_needed": 0, "approved": 0}
        out_total = 0.0
        for t in transactions:
            amount = float(t["amount"])
            out_total += -amount
            marked = self.db.get_state(f"no_receipt:{t['url']}")
            explanation = self._freeagent_explanation(t)
            approved = bool(explanation and explanation["approved"])
            receipt = None
            if t["url"] in filed_by_app or (t.get("explanation_attachments") or 0) > 0:
                kind = "filed"
                receipt = filed_by_app.get(t["url"])
            elif t["url"] in in_match:
                kind = "in_match"
                receipt = in_match[t["url"]][0]
            elif marked:
                kind = "not_needed"
            elif approved:
                kind = "approved"           # approved in FreeAgent, no receipt: done
            else:
                kind = "missing"
            counts[kind] += 1
            suggestion = None
            if kind == "missing":
                suggestion = self.email_suggestion(t["url"]) or self._ignored_suggestion(t)
            rows.append({
                "url": t["url"], "date": t["dated_on"], "description": (t["description"] or "").split("//")[0],
                "amount": amount, "status": kind, "approved": approved,
                "detail": (marked or "") if kind == "not_needed" else "",
                "receipt": None if receipt is None else {"id": receipt["id"], "vendor": receipt["vendor"],
                                                         "date": receipt["purchased_on"],
                                                         # shown as Files shows it: a photo scaled, a PDF as its pages
                                                         "is_image": bool(receipt["pdf_path"]) and Path(receipt["pdf_path"]).suffix.lower() in IMAGE_TYPES,
                                                         "page_count": self._page_count(receipt),
                                                         "source": receipt["source"] if "source" in receipt.keys() else "gmail",
                                                         "tidy": self._tidy_json(receipt),
                                                         **(self._review_line(*in_match[t["url"]])
                                                            if kind == "in_match" else {})},
                "suggestion": suggestion,
                # the payment's own FreeAgent settings, and FreeAgent's explanation if it has one
                "settings": self.payment_settings(t["url"]),
                "freeagent": {**(self._freeagent_explanation(t) or {}),
                              "explained": self._freeagent_explanation(t) is not None,
                              # you changed something that isn't in FreeAgent yet
                              "changes": self._explanation_changes(t, self.payment_settings(t["url"]))},
                "explained_here": json.loads(self.db.get_state(f"explained:{t['url']}") or "null"),
            })
        payments = counts["filed"] + counts["in_match"] + counts["missing"] + counts["approved"]
        return {
            "account": account,
            "account_name": accounts.get(account, {}).get("name", ""),
            "month": month,
            "limit": limit if every else None,
            "total_payments": total_payments,       # all of them, however many are shown
            "last_sync": self.db.get_state("freeagent:last_sync"),
            "rows": rows,
            "summary": {**counts, "payments": payments, "with_receipt": counts["filed"] + counts["in_match"],
                        "out": round(out_total, 2)},
        }

    def _review_line(self, row: Any, match: Any) -> dict[str, str]:
        """A paired receipt's group and reason, for its Statement line."""
        reference = self._freeagent_reference()
        names = {c["url"]: c["description"] for c in reference.get("categories", [])}
        registered = bool(reference.get("vat", {}).get("registered"))
        r = review(row, match, self._context(row, names, registered, match), overdue=self._overdue(row, match))
        return {"group": r["group"], "reason": r["reason"]}

    # ---- a payment's own FreeAgent settings (Statement) -------------------
    # Category, VAT and re-billing belong to the payment: set them on its
    # Statement line, then "No receipt needed" explains it in FreeAgent.

    PAYMENT_VAT_RATES = ("20.0", "5.0", "0.0")

    def payment_settings(self, transaction_url: str) -> dict[str, Any]:
        try:
            return json.loads(self.db.get_state(f"payment:{transaction_url}") or "{}")
        except ValueError:
            return {}

    def set_payment_settings(self, transaction_url: str, changes: dict[str, Any]) -> dict[str, Any]:
        unknown = set(changes) - {"category", "vat_rate", "rebill"}
        if unknown:
            raise ValueError(f"can't change {sorted(unknown)}")
        settings = self.payment_settings(transaction_url)
        if "category" in changes:
            known = {c["url"] for c in self._freeagent_reference().get("categories", [])}
            if changes["category"] and changes["category"] not in known:
                raise ValueError("unknown category")
            settings["category"] = changes["category"] or None
        if "vat_rate" in changes:
            if changes["vat_rate"] not in (None, "", *self.PAYMENT_VAT_RATES):
                raise ValueError("vat_rate must be 20.0, 5.0 or 0.0")
            settings["vat_rate"] = changes["vat_rate"] or None
        if "rebill" in changes:
            settings["rebill"] = self._valid_rebill(changes["rebill"])
        # a value set to nothing is kept: on a payment FreeAgent explained it
        # means "take that off" (stop re-billing), not "as it was"
        if settings:
            self.db.set_state(f"payment:{transaction_url}", json.dumps(settings))
        else:
            self.db.delete_state(f"payment:{transaction_url}")
        self._bump()
        return settings

    @staticmethod
    def _freeagent_explanation(t: dict[str, Any]) -> dict[str, Any] | None:
        """FreeAgent's own explanation of a payment, in the Statement's terms
        (VAT rate as "20.0"; markup as a percent), or None."""
        if not (t.get("explanation_url") and not t.get("unexplained_amount")):
            return None
        try:
            raw = json.loads(t.get("explanation_json") or "{}")
        except ValueError:
            raw = {}
        rate = raw.get("sales_tax_rate")
        rebill = None
        if raw.get("project"):
            # a project with no rebill_type is linked but not re-billed
            kind = raw.get("rebill_type") or "none"
            factor = raw.get("rebill_factor")
            if factor not in (None, ""):
                factor = round(float(factor) * 100, 2) if kind == "markup" else round(float(factor), 2)
            rebill = {"project": raw["project"], "type": kind, "factor": factor if kind in ("markup", "price") else None}
        return {"category": raw.get("category") or t.get("explanation_category"),
                "vat_rate": f"{float(rate):.1f}" if rate not in (None, "") else None,
                "rebill": rebill,
                # approved in FreeAgent (not a guess waiting for review)
                "approved": raw.get("marked_for_review") is False,
                # a guess waiting for approval: filing replaces it (_replace_guess)
                "guess": raw.get("marked_for_review") is True}

    def _explanation_changes(self, t: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        """What you changed on a payment FreeAgent explained, as FreeAgent fields."""
        from .filer import _apply_rebill
        fa = self._freeagent_explanation(t)
        if fa is None:
            return {}
        changes: dict[str, Any] = {}
        if settings.get("category") and settings["category"] != fa["category"]:
            changes["category"] = settings["category"]
        if settings.get("vat_rate") and settings["vat_rate"] != fa["vat_rate"]:
            changes["sales_tax_rate"] = settings["vat_rate"]
        if "rebill" in settings and settings["rebill"] != fa["rebill"]:
            fields: dict[str, Any] = {"project": None, "rebill_type": None, "rebill_factor": None}
            if settings["rebill"]:
                _apply_rebill([fields], settings["rebill"])
            changes.update(fields)
        return changes

    def update_payment_explanation(self, transaction_url: str) -> bool:
        return self._enqueue(f"file:{transaction_url}", lambda: self._run_update_payment_explanation(transaction_url))

    def _run_update_payment_explanation(self, transaction_url: str, live: bool = False) -> None:
        from .filer import FilingError as _FilingError, update_existing_explanation
        t = next((dict(x) for x in self.db.bank_transactions(self._freeagent_accounts()) if x["url"] == transaction_url), None)
        if t is None or self._freeagent_explanation(t) is None:
            self._finish("file", False, "That payment isn't explained in FreeAgent any more: refresh")
            return
        changes = self._explanation_changes(t, self.payment_settings(transaction_url))
        if not changes:
            self._finish("file", True, "Nothing to change")
            return
        if self.freeagent_dry_run and not live:
            self.db.set_state(f"explained:{transaction_url}", json.dumps(
                {"state": "dry_run", "kind": "update", "body": changes, "at": _now()}))
            self._finish("file", True, "Dry run: the change was prepared, nothing sent")
            return
        client = self._freeagent()
        if client is None or not client.connected:
            self._finish("file", False, "Connect FreeAgent first")
            return
        try:
            before = update_existing_explanation(client, transaction_url, t["explanation_url"], changes)
        except (_FilingError, FreeAgentError) as exc:
            self._finish("file", False, f"Not changed: {exc}")
            return
        self.db.set_state(f"explained:{transaction_url}", json.dumps(
            {"state": "updated", "url": t["explanation_url"], "before": before, "body": changes, "at": _now()}))
        self.db.delete_state(f"payment:{transaction_url}")       # FreeAgent now has them
        self._finish("file", True, f"Updated FreeAgent's explanation of the {t['dated_on']} payment")
        self._run_freeagent_sync()

    def reset_payment_settings(self, transaction_url: str) -> None:
        self.db.delete_state(f"payment:{transaction_url}")
        self._bump()

    def _payment_explanation(self, t: dict[str, Any], settings: dict[str, Any],
                             reason: str | None) -> tuple[dict[str, Any], list[str]]:
        """The explanation "No receipt needed" would create, and what stops it."""
        from .filer import _apply_rebill
        problems = [] if settings.get("category") else ["Choose a category"]
        description = (t.get("description") or "").split("//")[0].strip() or "Payment"
        body: dict[str, Any] = {
            "bank_transaction": t["url"], "dated_on": t["dated_on"], "gross_value": f"{float(t['amount']):.2f}",
            "category": settings.get("category"),
            "description": f"{description} (no receipt{': ' + reason if reason else ''})"[:200],
        }
        if self._freeagent_reference().get("vat", {}).get("registered") and settings.get("vat_rate"):
            body["sales_tax_rate"] = settings["vat_rate"]
        problems += _apply_rebill([body], settings.get("rebill"))
        return body, problems

    # ---- approving and removing explanations ------------------------------

    def _cached_transaction(self, transaction_url: str) -> dict[str, Any] | None:
        return next((dict(x) for x in self.db.bank_transactions(self._freeagent_accounts())
                     if x["url"] == transaction_url), None)

    def approve_payment(self, transaction_url: str, receipt_id: int | None = None) -> bool:
        """"Approve": the payment explained and approved in FreeAgent, with
        its receipt attached when there is one. Always sent, never a dry run
        (your choice, 2026-10-07)."""
        return self._enqueue(f"file:{transaction_url}", lambda: self._run_approve(transaction_url, receipt_id))

    def _run_approve(self, transaction_url: str, receipt_id: int | None) -> None:
        t = self._cached_transaction(transaction_url)
        if t is None:
            self._finish("file", False, "That payment isn't in the statement any more")
            return
        fa = self._freeagent_explanation(t)
        if receipt_id is not None:
            row = self.db.get_receipt(receipt_id)
            if row is not None and row["status"] == FILED and fa and fa["guess"]:
                # linked to a guess before Link approved: take it off, then file it properly
                client = self._freeagent()
                if client is None or not client.connected:
                    self._finish("file", False, "Connect FreeAgent first")
                    return
                try:
                    self._note(unfile(client, self.db, receipt_id))
                except (FilingError, FreeAgentError) as exc:
                    self._finish("file", False, f"Not approved: {exc}")
                    return
                self._run_freeagent_sync()
            self._run_file([receipt_id], live=True)      # replaces a guess (see _replace_guess)
            return
        if fa is None:
            self._run_explain_payment(transaction_url, None, live=True)
            return
        if not fa["guess"]:
            # already approved: Approve sends what you changed, if anything
            if self._explanation_changes(t, self.payment_settings(transaction_url)):
                self._run_update_payment_explanation(transaction_url, live=True)
            else:
                self._finish("file", True, "Already approved in FreeAgent")
            return
        self._set_activity(busy=True, kind="file", label="Approving in FreeAgent", started_at=_now(), log=[])
        client = self._freeagent()
        if client is None or not client.connected:
            self._finish("file", False, "Connect FreeAgent first")
            return
        from .filer import create_explanation, restore_body
        changes = self._explanation_changes(t, self.payment_settings(transaction_url))

        def make(guess: dict[str, Any]) -> str:
            body = {**restore_body(guess), **changes, "bank_transaction": transaction_url}
            url = create_explanation(client, body)
            self.db.set_state(f"explained:{transaction_url}",
                              json.dumps({"state": "filed", "url": url, "body": body, "at": _now()}))
            self.db.set_state(f"no_receipt:{transaction_url}", "Approved without a receipt")
            return f"Approved the {t['dated_on']} payment in FreeAgent"

        try:
            line = self._replace_guess(client, t, make, with_receipt=False)
        except (FilingError, FreeAgentError) as exc:
            self._finish("file", False, f"Not approved: {exc}")
            return
        self.db.delete_state(f"payment:{transaction_url}")       # FreeAgent now has them
        self._finish("file", True, line)
        self._run_freeagent_sync()

    def _replace_guess(self, client: Any, t: dict[str, Any], make: Callable[[dict[str, Any]], str],
                       with_receipt: bool) -> str:
        """Approve a payment FreeAgent guessed. Its API can't approve one
        (`marked_for_review` is read only), and linking a receipt to a guess
        only attached it and left it waiting. So the guess is checked,
        removed, and `make(guess)` explains the payment again: FreeAgent
        doesn't mark the app's own explanations for approval. If `make`
        fails, the guess is put back."""
        from .filer import NOT_SPENDING, create_explanation, removable_explanation, remove_explanation, restore_body
        guess = removable_explanation(client, t["url"], t["explanation_url"])
        if with_receipt and any(guess.get(k) for k in NOT_SPENDING):
            raise FilingError("FreeAgent explained this as a transfer or a payment of an invoice, not "
                              "spending. Approve it in FreeAgent.")
        # what's about to go, in case the app stops between the two steps
        self.db.set_state(f"replacing:{t['url']}", json.dumps({"guess": guess, "at": _now()}))
        remove_explanation(client, guess)
        try:
            line = make(guess)
        except (FilingError, FreeAgentError, OSError) as exc:
            try:
                create_explanation(client, restore_body(guess))
                put_back = "FreeAgent's explanation was put back."
            except FreeAgentError:
                put_back = "Putting FreeAgent's explanation back failed too: explain it in FreeAgent."
            raise FilingError(f"{exc}. {put_back}") from exc
        self.db.delete_state(f"replacing:{t['url']}")
        return line

    def remove_payment_explanation(self, transaction_url: str) -> bool:
        return self._enqueue(f"file:{transaction_url}", lambda: self._run_remove_explanation(transaction_url))

    def _run_remove_explanation(self, transaction_url: str) -> None:
        """"Remove explanation": FreeAgent's explanation of a payment deleted,
        so it's unexplained again. What it said is kept (removed:<url>)."""
        from .filer import removable_explanation, remove_explanation
        t = self._cached_transaction(transaction_url)
        if t is None or not t.get("explanation_url"):
            self._finish("file", False, "That payment has no explanation in FreeAgent")
            return
        client = self._freeagent()
        if client is None or not client.connected:
            self._finish("file", False, "Connect FreeAgent first")
            return
        try:
            current = removable_explanation(client, transaction_url, t["explanation_url"])
            self.db.set_state(f"removed:{transaction_url}", json.dumps({"explanation": current, "at": _now()}))
            remove_explanation(client, current)
        except (FilingError, FreeAgentError) as exc:
            self._finish("file", False, f"Not removed: {exc}")
            return
        for key in ("explained", "no_receipt", "payment"):
            self.db.delete_state(f"{key}:{transaction_url}")
        self._finish("file", True, f"Removed FreeAgent's explanation of the {t['dated_on']} payment")
        self._run_freeagent_sync()

    def explain_payment(self, transaction_url: str, reason: str | None) -> bool:
        return self._enqueue(f"file:{transaction_url}", lambda: self._run_explain_payment(transaction_url, reason))

    def _run_explain_payment(self, transaction_url: str, reason: str | None, live: bool = False) -> None:
        from .filer import FilingError as _FilingError, explain_without_receipt
        t = next((dict(x) for x in self.db.bank_transactions(self._freeagent_accounts()) if x["url"] == transaction_url), None)
        if t is None:
            self._finish("file", False, "That payment isn't in the statement any more")
            return
        body, problems = self._payment_explanation(t, self.payment_settings(transaction_url), reason)
        if problems:
            self._finish("file", False, "; ".join(problems))
            return
        if self.freeagent_dry_run and not live:
            self.db.set_state(f"explained:{transaction_url}", json.dumps({"state": "dry_run", "body": body, "at": _now()}))
            self._finish("file", True, "Dry run: the explanation was prepared, nothing sent")
            return
        client = self._freeagent()
        if client is None or not client.connected:
            self._finish("file", False, "Connect FreeAgent first")
            return
        try:
            url = explain_without_receipt(client, transaction_url, body)
        except (_FilingError, FreeAgentError) as exc:
            self._finish("file", False, f"Not explained: {exc}")
            return
        self.db.set_state(f"explained:{transaction_url}", json.dumps({"state": "filed", "url": url, "body": body, "at": _now()}))
        self.db.set_state(f"no_receipt:{transaction_url}", (reason or "Explained without a receipt")[:80])
        self._finish("file", True, f"Explained the {t['dated_on']} payment in FreeAgent, no receipt")
        self._run_freeagent_sync()

    def unexplain_payment(self, transaction_url: str) -> bool:
        return self._enqueue(f"unfile:{transaction_url}", lambda: self._run_unexplain_payment(transaction_url))

    def _run_unexplain_payment(self, transaction_url: str) -> None:
        state = json.loads(self.db.get_state(f"explained:{transaction_url}") or "{}")
        if state.get("state") == "updated" and state.get("url"):
            # we changed FreeAgent's own explanation: put back what it had
            client = self._freeagent()
            if client is None or not client.connected:
                self._finish("unfile", False, "Connect FreeAgent first")
                return
            client.writes_allowed = True
            try:
                client.update_explanation(state["url"], state.get("before") or {})
            except FreeAgentError as exc:
                self._finish("unfile", False, f"Not undone: {exc}")
                return
            finally:
                client.writes_allowed = False
            self.db.delete_state(f"explained:{transaction_url}")
            self._finish("unfile", True, "Put FreeAgent's explanation back as it was")
            self._run_freeagent_sync()
            return
        if state.get("state") == "filed" and state.get("url"):
            client = self._freeagent()
            if client is None or not client.connected:
                self._finish("unfile", False, "Connect FreeAgent first")
                return
            client.writes_allowed = True
            try:
                client.delete(state["url"])
            except FreeAgentError as exc:
                if "(404)" not in str(exc):
                    self._finish("unfile", False, f"Not undone: {exc}")
                    return
            finally:
                client.writes_allowed = False
        self.db.delete_state(f"explained:{transaction_url}")
        self.db.delete_state(f"no_receipt:{transaction_url}")
        self._finish("unfile", True, "Removed the explanation from FreeAgent")
        self._run_freeagent_sync()

    def mark_no_receipt(self, transaction_url: str, reason: str | None) -> None:
        """"No receipt needed" on a statement line (a tax payment, a
        transfer), or None to undo."""
        if reason:
            self.db.set_state(f"no_receipt:{transaction_url}", str(reason)[:80])
        else:
            self.db.delete_state(f"no_receipt:{transaction_url}")
        self._bump()

    UPLOAD_TYPES = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".pdf")
    UPLOAD_MAX_BYTES = 30 * 1024 * 1024

    def upload_file(self, name: str, data: bytes, paid_by: str | None = "business") -> str:
        """A file dropped on Files (or Expenses): into the receipt inbox, read
        straight away like a phone photo."""
        suffix = Path(name or "").suffix.lower()
        if suffix not in self.UPLOAD_TYPES:
            raise ValueError("Only photos (JPEG, PNG, HEIC, WebP) and PDFs can be added")
        if not data:
            raise ValueError("That file is empty")
        if len(data) > self.UPLOAD_MAX_BYTES:
            raise ValueError("That file is over 30 MB")
        folder = self.photo_inbox / {"personal": "Expense", "business": "Bank"}.get(paid_by or "", "")
        folder.mkdir(parents=True, exist_ok=True)
        stem = "".join(ch for ch in Path(name).stem if ch.isalnum() or ch in " -_")[:60].strip() or "receipt"
        target = folder / f"{datetime.now():%Y-%m-%d %H%M%S} {stem}{suffix}"
        partial = target.with_name("." + target.name + ".part")
        partial.write_bytes(data)
        # written whole, so no need to wait for it to "settle" like a synced file
        settled = time.time() - 60
        os.utime(partial, (settled, settled))
        partial.rename(target)
        self.check_photos()
        self._bump()
        return str(target)

    def add_receipt_file(self, path: str, paid_by: str = "business") -> str:
        """Copy a chosen file into the receipt inbox ("Add photo" on a
        statement line); the inbox reads it like any other."""
        source = Path(path)
        if not source.is_file():
            raise ValueError("no such file")
        folder = self.photo_inbox / ("Expense" if paid_by == "personal" else "Bank")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{datetime.now():%Y-%m-%d %H%M%S} shared{source.suffix.lower()}"
        import shutil
        shutil.copy2(source, target)
        self._bump()
        return str(target)

    # ---- "Use that email" (PLAN.md §11): a receipt for a payment without one

    def _missing_payments(self, days: int = EMAIL_HINT_DAYS) -> list[dict[str, Any]]:
        """Payments out, in the chosen accounts, over the last `days`, with no
        receipt anywhere: not filed, not paired in Match, not marked "no
        receipt needed"."""
        chosen = self._freeagent_accounts()
        if not chosen:
            return []
        since = (datetime.now() - timedelta(days=days)).date().isoformat()
        pending = self.db.list_receipts(PENDING)
        paired = {m.transaction["url"] for m in self._matches(pending).values() if m.transaction is not None}
        filed = set()
        for row in self.db.list_receipts(FILED):
            state = json.loads(row["freeagent_json"] or "{}") if row["freeagent_json"] else {}
            if state.get("transaction"):
                filed.add(state["transaction"])
        accounts = {a["url"]: a for a in self._freeagent_reference().get("bank_accounts", [])}
        out = []
        for t in self.db.bank_transactions(chosen):
            t = dict(t)
            if (t["dated_on"] < since or float(t["amount"]) >= 0 or t["url"] in paired or t["url"] in filed
                    or (t.get("explanation_attachments") or 0) > 0
                    or self.db.get_state(f"no_receipt:{t['url']}")):
                continue
            t["currency"] = accounts.get(t["bank_account"], {}).get("currency", "GBP")
            out.append(t)
        return out

    def _ignored_suggestion(self, t: dict[str, Any]) -> dict[str, Any] | None:
        """A receipt you ignored with this payment's exact amount, near its date."""
        day = date.fromisoformat(t["dated_on"][:10])
        for row in self.db.list_receipts(IGNORED):
            if row["total"] is None or round(float(row["total"]), 2) != round(-float(t["amount"]), 2):
                continue
            when = row["purchased_on"] or row["email_date"]
            try:
                gap = (day - date.fromisoformat(when[:10])).days if when else 99
            except ValueError:
                continue
            if -2 <= gap <= 7:
                return {"kind": "ignored", "id": row["id"], "vendor": row["vendor"], "date": when[:10],
                        "label": f"You ignored a {row['vendor'] or 'receipt'} receipt for "
                                 f"{_money_text(row['total'], row['currency'])} on {_day_text(when)}."}
        return None

    def email_suggestion(self, transaction_url: str) -> dict[str, Any] | None:
        try:
            found = json.loads(self.db.get_state(f"email_hint:{transaction_url}") or "null")
        except ValueError:
            return None
        if not found or not found.get("message_id"):
            return None
        return {"kind": "email", **found,
                "label": f"Found {_a_or_an(found['supplier'])} {found['supplier']} email for "
                         f"{_money_text(found['amount'], found.get('currency', 'GBP'))} on {_day_text(found['day'])}."}

    def find_emails(self) -> bool:
        """Look in Gmail for receipts for payments that have none (queued)."""
        if not self.accounts.list() or not self._freeagent_accounts():
            return False
        return self._enqueue("email-hints", self._run_find_emails)

    def _run_find_emails(self) -> None:
        from . import email_finder

        accounts = self.accounts.list()
        today = datetime.now().date()
        searched = found = 0
        for t in sorted(self._missing_payments(), key=lambda t: t["dated_on"], reverse=True):
            key = f"email_hint:{t['url']}"
            previous = json.loads(self.db.get_state(key) or "{}") if self.db.get_state(key) else {}
            if previous.get("message_id"):
                continue
            # one look a day, while an email might still turn up
            age = (today - date.fromisoformat(t["dated_on"][:10])).days
            if previous.get("checked") == today.isoformat() or (previous and age > EMAIL_HINT_RECHECK_DAYS):
                continue
            if searched >= EMAIL_HINT_SEARCHES:
                break
            searched += 1
            hits = []
            for account in accounts:
                try:
                    client = account.client(self.config.credentials_file, self.config.scopes)
                    for stub in client.search(email_finder.query(float(t["amount"]), t["dated_on"]), max_results=5):
                        if self.db.has_message(stub["id"]) and not self._is_ignored_message(stub["id"]):
                            continue                  # already a receipt (in Match, Filed…)
                        message = client.fetch(stub["id"], stub.get("threadId", ""))
                        hit = email_finder.judge(message, float(t["amount"]), account.email, t["currency"])
                        if hit:
                            hits.append(hit)
                except Exception as exc:              # one account's trouble isn't the others'
                    log.info("email search in %s: %s", account.email, exc)
            chosen = email_finder.best(hits, t["dated_on"])
            if chosen:
                found += 1
                self.db.set_state(key, json.dumps({**chosen.as_dict(), "currency": t["currency"]}))
            else:
                self.db.set_state(key, json.dumps({"checked": today.isoformat()}))
        if searched:
            log.info("email search: %d payment(s) looked for, %d email(s) found", searched, found)
        if found:
            self._bump()

    def _is_ignored_message(self, message_id: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute("SELECT status FROM receipts WHERE gmail_message_id = ?", (message_id,)).fetchone()
        return bool(row and row["status"] == IGNORED)

    def use_suggestion(self, transaction_url: str) -> bool:
        """"Use that email" / "Use that receipt" on a Statement line."""
        return self._enqueue(f"use-email:{transaction_url}", lambda: self._run_use_suggestion(transaction_url))

    def _run_use_suggestion(self, url: str) -> None:
        t = next((dict(t) for t in self.db.bank_transactions(self._freeagent_accounts()) if t["url"] == url), None)
        if t is None:
            self._finish("use-email", False, "That payment is no longer in the statement.")
            return
        ignored = self._ignored_suggestion(t)
        hint = self.email_suggestion(url)
        if hint and self._is_ignored_message(hint["message_id"]):
            with self.db.connect() as conn:
                row = conn.execute("SELECT id FROM receipts WHERE gmail_message_id = ?",
                                   (hint["message_id"],)).fetchone()
            ignored = {"id": row["id"]} if row else ignored
            hint = None
        if ignored and not hint:
            self.db.set_status(ignored["id"], PENDING)
            self.db.update_receipt(ignored["id"], {"transaction_url": url})
            self._finish("use-email", True, "The receipt is back in Match, paired with this payment.")
            return
        if not hint:
            self._finish("use-email", False, "No email found for this payment.")
            return
        self._set_activity(busy=True, kind="retry", label=f"Fetching the {hint['supplier']} email",
                           started_at=_now(), log=[])
        account = self.accounts.get(hint["account"])
        if account is None:
            self._finish("use-email", False, f"{hint['account']} is no longer connected.")
            return
        try:
            client = account.client(self.config.credentials_file, self.config.scopes)
            message = client.fetch(hint["message_id"], hint.get("thread_id", ""))
            path, source = self._email_document(message, hint)
        except Exception as exc:
            log.exception("use email")
            self._finish("use-email", False, f"Couldn't fetch that email: {exc}")
            return
        receipt_id = self.db.insert_receipt({
            "watcher_id": "email", "account": account.email,
            "gmail_message_id": message.message_id, "gmail_thread_id": message.thread_id,
            "vendor": hint["supplier"], "purchased_on": hint["day"] or t["dated_on"],
            "total": hint["amount"], "currency": hint.get("currency", "GBP"),
            "subject": message.subject, "email_date": message.date_iso,
            "pdf_path": str(path), "pdf_source": source,
            "filename": f"{hint['day']} {hint['supplier']} {hint.get('currency', 'GBP')}{hint['amount']:.2f}.pdf",
            "transaction_url": url, "status": PENDING,
            "extra_json": {"found_for_payment": url, "supplier_source": "sender"},
        })
        self.db.delete_state(f"email_hint:{url}")
        if receipt_id:
            self._finish("use-email", True, f"Added the {hint['supplier']} email to Match, paired with this payment.")
        else:
            self._finish("use-email", False, "That email is already a receipt.")

    def _email_document(self, message: Any, hint: dict[str, Any]) -> tuple[Path, str]:
        """The email's own PDF if it has one, else the email printed."""
        from .browser import BrowserPool
        from .pdf import render_email_to_pdf
        from .pipeline import _safe_stem

        out = self.config.pdf_dir / f"{_safe_stem(message.message_id)}.pdf"
        out.parent.mkdir(parents=True, exist_ok=True)
        attached = [a for a in message.pdf_attachments() if a.get("data")]
        if attached:
            out.write_bytes(attached[0]["data"])
            return out, "attachment"
        import html as html_lib
        body = message.html_with_images() or f"<pre>{html_lib.escape(message.plain or '')}</pre>"
        pool = BrowserPool(headless=self.config.headless,
                           block_trackers=bool(self.config.pdf.get("block_trackers", True)))
        try:
            render_email_to_pdf(pool, body, {"vendor": hint["supplier"], "total": hint["amount"],
                                             "purchased_on": hint["day"]}, out, self.config.pdf)
        finally:
            pool.close()
        return out, "rendered_email"

    # ---- the Emails view: any one email into Files ------------------------
    #
    # Unlike the rest of the UI these talk to Gmail while you wait, like the
    # supplier search: you're looking through your mail, so there is nothing
    # to show until Gmail answers. Each request has its own client (they
    # aren't safe across threads) and gives up after REQUEST_TIMEOUT.

    def _mail_client(self, account: str = "") -> tuple[Any, Any]:
        chosen = self.accounts.get(account) if account else None
        chosen = chosen or next(iter(self.accounts.list()), None)
        if chosen is None:
            raise ValueError("Connect a Gmail account first.")
        return chosen, chosen.client(self.config.credentials_file, self.config.scopes)

    def list_emails(self, account: str = "", search: str = "", receipts_only: bool = False,
                    page_token: str = "") -> dict[str, Any]:
        """One page of the mailbox, newest first, each email judged for how
        much it looks like a receipt."""
        from . import email_inbox

        chosen, client = self._mail_client(account)
        query = email_inbox.list_query(search, receipts_only)
        rows: list[dict[str, Any]] = []
        next_token, pages = page_token, 0
        while True:
            stubs, next_token = client.search_page(query, next_token, EMAIL_PAGE)
            page = [email_inbox.list_row(h, chosen.email) for h in client.headers([s["id"] for s in stubs])] \
                if stubs else []
            if receipts_only:
                # Gmail's word search finds "receipt" in "receipt-bridge" and
                # anywhere in a body: only what the app also judges a receipt
                page = [r for r in page if r["receipt"]]
            rows += page
            pages += 1
            # a thinned-out page reads on, so the list isn't nearly empty
            if not receipts_only or not next_token or len(rows) >= EMAIL_PAGE // 2 or pages >= EMAIL_RECEIPT_PAGES:
                break
        known = self.emails_in_files([r["id"] for r in rows])
        for row in rows:
            row["in_files"] = known.get(row["id"])
        return {"account": chosen.email, "emails": rows, "next": next_token}

    def supplier_names(self, limit: int = 500) -> list[str]:
        """Suppliers you've had before, most used first, for the supplier box
        to suggest. Spelled as most recently written; local only."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT vendor, COUNT(*) AS n, MAX(COALESCE(purchased_on, email_date, created_at)) AS last "
                "FROM receipts WHERE vendor IS NOT NULL AND TRIM(vendor) != '' AND status != 'deleted' "
                "GROUP BY vendor ORDER BY n DESC, last DESC").fetchall()
        # One suggestion per supplier: "Boot", "Boots" and "Boots Ltd" are one,
        # offered as the spelling you've used most, ranked by all of them
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = " ".join(str(row["vendor"]).split())
            group = groups.setdefault(_supplier_key_for_names(name), {"spellings": [], "n": 0, "last": ""})
            group["n"] += row["n"]
            group["last"] = max(group["last"], row["last"] or "")
            # the spelling offered: the most used, then the latest, then one
            # without "Ltd", then the fuller ("Boots", not "Boot")
            bare = _LEGAL_WORDS.sub("", name).strip(" .,&")
            group["spellings"].append(((row["n"], row["last"] or "", bare == name, len(bare)), name))
        ranked = sorted(groups.values(), key=lambda g: (g["n"], g["last"]), reverse=True)
        return [max(g["spellings"])[1] for g in ranked][:limit]

    def emails_in_files(self, message_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Which of these emails are already receipts, and where they are.
        Local only: the list asks again after each change, without Gmail."""
        ids = [m for m in message_ids if m][:500]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self.db.connect() as conn:
            rows = conn.execute(f"SELECT id, gmail_message_id, status, vendor FROM receipts "
                                f"WHERE gmail_message_id IN ({marks})", ids).fetchall()
        return {r["gmail_message_id"]: {"id": r["id"], "status": r["status"], "vendor": r["vendor"]}
                for r in rows}

    def _cached_email(self, account: Any, client: Any, message_id: str) -> Any:
        key = (account.email, message_id)
        with self._lock:
            cache = self._email_cache
            if key in cache:
                cache[key] = cache.pop(key)          # most recently used last
                return cache[key]
        message = client.fetch(message_id)
        with self._lock:
            cache[key] = message
            while len(cache) > EMAILS_CACHED:
                cache.pop(next(iter(cache)))
        return message

    def open_email(self, account: str, message_id: str) -> dict[str, Any]:
        """One whole email for the Emails view, with what it would become."""
        from email.utils import parseaddr

        from . import email_inbox

        chosen, client = self._mail_client(account)
        message = self._cached_email(chosen, client, message_id)
        display, address = parseaddr(message.sender or "")
        attachments = [{"filename": a.get("filename") or "attachment", "content_type": a.get("content_type", ""),
                        "size": a.get("size", 0)} for a in message.attachments]
        return {
            "id": message_id,
            "thread_id": message.thread_id,
            "account": chosen.email,
            "subject": message.subject,
            "from_name": display or address,
            "from_address": address,
            "to": message.recipient,
            "date": message.date.isoformat() if message.date else "",
            "html": message.html_with_images(),
            "text": message.text,
            "attachments": attachments,
            "draft": email_inbox.draft(message),
            "in_files": self.emails_in_files([message_id]).get(message_id),
        }

    def email_pdf(self, account: str, message_id: str) -> tuple[str, bytes] | None:
        """The PDF an email carries, which becomes the receipt: (filename,
        bytes), or None. Shown beside "Convert to receipt"; the email is in
        memory already, from being opened."""
        chosen, client = self._mail_client(account)
        message = self._cached_email(chosen, client, message_id)
        attached = [a for a in message.pdf_attachments() if a.get("data")]
        return (attached[0].get("filename") or "receipt.pdf", attached[0]["data"]) if attached else None

    def add_email(self, account: str, message_id: str, fields: dict[str, Any]) -> bool:
        """"Add to Files" on an email: queued, as printing it takes a browser.
        `fields` is what the form shows (supplier, date, total, currency,
        vat, paid_by), checked here so a mistake is said at once."""
        clean = _email_fields(fields)
        known = self.emails_in_files([message_id]).get(message_id)
        if known and known["status"] in (PENDING, EXPORTED, FILED):
            raise ValueError("That email is already a receipt.")
        chosen, _ = self._mail_client(account)
        return self._enqueue(f"email-add:{message_id}",
                             lambda: self._run_add_email(chosen.email, message_id, clean))

    def _run_add_email(self, account: str, message_id: str, fields: dict[str, Any]) -> None:
        supplier = fields["supplier"] or "Email"
        self._set_activity(busy=True, kind="retry", label=f"Adding the {supplier} email",
                           started_at=_now(), log=[])
        try:
            chosen, client = self._mail_client(account)
            message = self._cached_email(chosen, client, message_id)
            path, source = self._email_document(message, {"supplier": supplier, "amount": fields["total"],
                                                          "day": fields["date"]})
        except Exception as exc:
            log.exception("add email")
            self._finish("email-add", False, f"Couldn't add that email: {exc}")
            return
        money = f" {fields['currency']}{fields['total']:.2f}" if fields["total"] is not None else ""
        day = fields["date"] or message.date_iso
        data = {
            "watcher_id": "email", "account": chosen.email,
            "gmail_message_id": message_id, "gmail_thread_id": message.thread_id,
            "vendor": supplier, "purchased_on": day or None,
            "total": fields["total"], "currency": fields["currency"], "vat": fields["vat"],
            "subject": message.subject, "email_date": message.date_iso,
            "pdf_path": str(path), "pdf_source": source,
            # typed, or from the sender's name: made safe, as exports are written by it
            "filename": build_filename("{day} {supplier}{money}", {"day": day, "supplier": supplier, "money": money}),
            "paid_by": fields["paid_by"], "status": PENDING,
            "extra_json": {"added_from": "emails", "supplier_source": "sender", "vat_choice": fields["vat_choice"],
                           **({"vat_amount": fields["vat"]} if fields["vat_choice"] == "amount" else {})},
        }
        known = self.emails_in_files([message_id]).get(message_id)
        if known and known["status"] in (IGNORED, FAILED, DELETED):
            # chosen by hand now: what was ignored or couldn't be read comes back
            data.pop("status")
            self.db.update_receipt(known["id"], {**data, "error": None})
            self.db.set_status(known["id"], PENDING)
            receipt_id = known["id"]
        else:
            receipt_id = self.db.insert_receipt(data)
        if not receipt_id:
            self._finish("email-add", False, "That email is already a receipt.")
            return
        where = "an expense" if fields["paid_by"] == "personal" else "a receipt"
        self._finish("email-add", True, f"Added the {supplier} email to Files as {where}.")

    def _suppliers_with_better_documents(self) -> set[str]:
        """Suppliers whose rule can get something better than the email.

        For those, an email printout means the better document was missed —
        worth flagging and retrying. For the rest (Yesim, say) the email *is*
        the receipt, and flagging it as a problem would be crying wolf.
        """
        watchers, _ = load_watchers_safe(self.config.watchers_dir)
        return {w.id for w in watchers if _has_better_source(w)}

    @staticmethod
    def _tidy_json(row: Any) -> dict[str, Any] | None:
        """How the photo shown was tidied, for the note under it and its
        "Show plain photo" switch. None for anything that wasn't offered."""
        try:
            tidy = (json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}).get("tidy")
        except ValueError:
            return None
        if not tidy:
            return None
        return {"on": bool(row["pdf_path"]) and row["pdf_path"] == tidy.get("tidied_path"),
                "available": bool(tidy.get("tidied_path")) and Path(tidy["tidied_path"]).exists(),
                "steps": tidy.get("steps") or [], "dropped": tidy.get("dropped") or [],
                "notes": tidy.get("notes") or []}

    def set_tidy(self, receipt_id: int, on: bool) -> None:
        """Show (and file) the tidied copy of a photo, or the plain one.
        Only the picture and where the highlights sit change: the tidied
        copy was only kept because it read the same."""
        row = self.db.get_receipt(receipt_id)
        if row is None:
            raise ValueError("no such receipt")
        if row["status"] == FILED:
            raise ValueError("already filed to FreeAgent")
        extra = json.loads(row["extra_json"] or "{}") if row["extra_json"] else {}
        tidy = extra.get("tidy") or {}
        path = tidy.get("tidied_path") if on else tidy.get("plain_path")
        if not path or not Path(path).exists():
            raise ValueError("that copy of the photo isn't there")
        extra["layout"] = tidy.get("tidied_layout" if on else "plain_layout") or []
        self.db.update_receipt(receipt_id, {"pdf_path": path, "extra_json": json.dumps(extra)})
        self._bump()

    def original_path(self, receipt_id: int) -> Path | None:
        """The photo exactly as it arrived (data/photos/), untouched."""
        record = self.db.get_receipt(receipt_id)
        if record is None or record["source"] != "photo" or not record["original_path"]:
            return None
        path = Path(record["original_path"])
        return path if path.exists() else None

    def pdf_path(self, receipt_id: int) -> Path | None:
        record = self.db.get_receipt(receipt_id)
        if record is None or not record["pdf_path"]:
            return None
        path = Path(record["pdf_path"])
        return path if path.exists() else None

    # ---- the snapshot the UI polls --------------------------------------

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def review_counts(self) -> dict[str, int]:
        """The sidebar numbers: receipts that need you (Match) and payments
        this month missing a receipt (Statement). Worked out once per change
        (and once a day, as waiting receipts become overdue)."""
        key = (self.version, datetime.now().date())
        cached = self._review_counts_cache
        if cached and cached[0] == key:
            return cached[1]
        counts = {"needs": 0, "missing": 0, "files": 0}
        try:
            pending = self.receipts(PENDING)
            counts["needs"] = sum(1 for r in pending if r.get("group") == "needs")
            counts["files"] = len(pending)          # everything not filed yet, expenses included
            counts["missing"] = self.statement(None, None).get("summary", {}).get("missing", 0)
        except Exception as exc:          # the sidebar must never break the snapshot
            log.warning("sidebar counts: %s", exc)
        self._review_counts_cache = (key, counts)
        return counts

    def snapshot(self) -> dict[str, Any]:
        """Everything the UI needs, from memory and the local database only."""
        with self._lock:
            activity = self._activity
            outcome = self._outcome
            health = dict(self._health)
            version = self._version
            queued = sorted(self._queued)

        counts = self.db.counts_by_status()
        accounts = []
        for account in self.accounts.list():
            h = health.get(account.email, Health())
            accounts.append(
                {
                    "email": account.email,
                    "state": h.state,
                    "detail": h.detail,
                    "checked_at": h.checked_at,
                    "read_only": account.read_only,
                    "receipts": self.db.count_for_account(account.email),
                    "last_scan_at": self.db.get_state(f"last_scan_at:{account.email}"),
                }
            )

        return {
            "version": version,
            "activity": {
                "busy": activity.busy,
                "kind": activity.kind,
                "label": activity.label,
                "current": activity.current,
                "total": activity.total,
                "started_at": activity.started_at,
                "log": activity.log[-60:],
            },
            "queued": queued,
            "outcome": outcome.__dict__ if outcome else None,
            "counts": {
                "pending": counts.get(PENDING, 0),
                "exported": counts.get(EXPORTED, 0) + counts.get(FILED, 0),
                "ignored": counts.get(IGNORED, 0),
                "failed": counts.get(FAILED, 0),
                **self.review_counts(),
            },
            "totals": {"pending": self.db.totals(PENDING)},
            "accounts": accounts,
            "needs_attention": [a for a in accounts if a["state"] in ("expired", "signed_out", "error")],
            "connecting": self._connecting,
            "notifications": self._notification_status(),
            "notification_prefs": self.notification_prefs(),
            "last_log": self._last_log[-200:],
            "last_scan_at": self._last_scan_at(),
            "auto_scan": self.db.get_state("pref:auto_scan", "1") == "1",
            "archive_delete_days": self.archive_delete_days,
            "open_at_login": {"enabled": login_item.is_enabled(), "available": login_item.bundle_path() is not None},
            "theme": self.theme,
            "export_dir": str(self.config.export_dir),
            "has_credentials": self.config.credentials_file.exists(),
            # the app keys come in a licence file dropped on the app (app/licence.py)
            "needs_licence": not (self.config.credentials_file.exists()
                                  and self.config.freeagent_credentials_file.exists()),
            "freeagent": self.freeagent_snapshot(),
            "update": self.update_snapshot(),
            "file_results": self._file_results,     # the last File / File all (docs/API.md)
            "photo_inbox": {
                "path": str(self.photo_inbox),
                "exists": self.photo_inbox.is_dir(),
                "has_subfolders": all((self.photo_inbox / s).is_dir() for s in ("Bank", "Expense")),
                "is_default": self.db.get_state("pref:photo_inbox") is None,
                "archive": str(self.photo_archive),
                "archive_is_default": self.db.get_state("pref:photo_archive") is None,
                "waiting_bytes": self._inbox_bytes,
                "too_big": self._inbox_bytes > INBOX_WARN_BYTES,
            },
            "setup": self.setup_snapshot(),
        }

    def _last_scan_at(self) -> str | None:
        """When the inbox was last checked.

        Falls back to the per-account times recorded before this key
        existed, so an upgrade doesn't claim the inbox was never checked.
        """
        latest = self.db.get_state("last_scan_finished_at")
        if latest:
            return latest
        stamps = list(self.db.list_state("last_scan_at:").values())
        return max(stamps) if stamps else None

    # ---- updates --------------------------------------------------------
    #
    # The check has its own short thread rather than the worker: a scan can
    # hold the worker for minutes, and "Check for updates" should answer in a
    # second or two. Installing does queue on the worker, so it never lands
    # in the middle of filing, and the app restarts straight after.

    def check_for_updates(self) -> bool:
        """Ask GitHub for the latest version, in the background. False if a
        check is already running."""
        with self._lock:
            if self._checking_updates:
                return False
            self._checking_updates = True
        self._bump()

        def run() -> None:
            result: dict[str, Any] = {"checked_at": _now()}
            try:
                result.update(updates.latest(self.config.update_repo))
            except updates.UpdateError as exc:
                result["error"] = str(exc)
            except Exception as exc:          # a bad answer must never stick the spinner
                log.exception("update check failed")
                result["error"] = f"Couldn't check for updates: {exc}"
            finally:
                self.db.set_state("update:last", json.dumps(result))
                with self._lock:
                    self._checking_updates = False
                self._bump()
            self._announce_update()

        threading.Thread(target=run, name="receipt-updates", daemon=True).start()
        return True

    def _announce_update(self) -> None:
        """One notification per new version, however often it's checked."""
        snap = self.update_snapshot()
        if not snap["available"] or self.db.get_state("update:notified") == snap["latest"]:
            return
        self.db.set_state("update:notified", snap["latest"])
        self.notify_about("updates", f"Version {snap['latest'].lstrip('vV')} is available. "
                                     "Open Receipt Bridge and click Update now.")

    def _last_update_check(self) -> dict[str, Any]:
        try:
            last = json.loads(self.db.get_state("update:last") or "{}")
        except ValueError:
            return {}
        return last if isinstance(last, dict) else {}

    def _update_check_due(self) -> bool:
        try:
            when = datetime.fromisoformat(self._last_update_check()["checked_at"])
        except (KeyError, TypeError, ValueError):
            return True
        return datetime.now(timezone.utc) - when >= UPDATE_CHECK_INTERVAL

    def update_snapshot(self) -> dict[str, Any]:
        last = self._last_update_check()
        latest = last.get("version") or ""
        installed = updates.on_disk_version() or updates.VERSION
        git = (updates.ROOT / ".git").exists()
        # GitHub has something newer than the files here: download or pull it
        behind = bool(latest) and updates.is_newer(latest, installed)
        # the files here are newer than what's running: only a restart is needed
        restart_needed = updates.is_newer(installed) and not behind
        with self._lock:
            installing = "update" in self._queued
        return {
            "current": updates.VERSION,
            "installed": installed,
            "latest": latest if behind or not restart_needed else installed,
            "available": behind or restart_needed,
            "restart_needed": restart_needed,
            "url": last.get("url") or "",
            "notes": last.get("notes") or "",
            "error": last.get("error") or "",
            "checked_at": last.get("checked_at"),
            "checking": self._checking_updates,
            "installing": installing,
            # a git checkout pulls; any other copy downloads and copies over
            "can_install": restart_needed or (behind and (git or bool(last.get("download")))),
            "git_checkout": git,
            "restarts": self.restart is not None,
            "repo": self.config.update_repo,
        }

    def install_update(self) -> bool:
        """Download (or pull) the latest version and restart, or only
        restart when the files here are already newer than what's running."""
        if not self.update_snapshot()["can_install"]:
            raise ValueError("There's no update to install. Check for updates first.")
        return self._enqueue("update", self._run_update)

    def _run_update(self) -> None:
        snap = self.update_snapshot()
        last = self._last_update_check()
        if not snap["restart_needed"]:
            target = snap["latest"].lstrip("vV")
            self._set_activity(busy=True, kind="update", label=f"Updating to {target}", started_at=_now(), log=[])
            try:
                if snap["git_checkout"]:
                    result = updates.pull(log=self._note)
                else:
                    result = updates.install(last["download"], last["version"], self.config.update_repo,
                                             log=self._note)
                # Only the copy in Applications rebuilds it: another copy (a test
                # build in dist/) would replace the real app with itself.
                if result["app"] and login_item.running_bundle() == str(login_item.INSTALLED):
                    self._note("Rebuilding the app")
                    login_item.install_app()
            except (updates.UpdateError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                log.warning("update failed: %s", exc)
                self._finish("update", False, f"Couldn't update: {exc}")
                return
        version = (updates.on_disk_version() or snap["latest"]).lstrip("vV")
        if self.restart is not None and self.restart():
            self._finish("update", True, f"Updated to {version}. Restarting…")
        else:
            self._finish("update", True, f"Updated to {version}. Quit and reopen Receipt Bridge to use it.")

    def install_licence(self, data: bytes) -> list[str]:
        """Save the keys in a dropped licence file. Raises LicenceError."""
        from .licence import install

        installed = install(data, self.config.credentials_file, self.config.freeagent_credentials_file)
        self._freeagent_error = self._freeagent_problem = ""
        self._bump()
        return installed

    def open_update_page(self) -> bool:
        """Open the latest version on GitHub in the browser. Only ever the
        address GitHub itself gave, never one the page supplies."""
        url = self.update_snapshot()["url"]
        if not url.startswith("https://github.com/"):
            return False
        subprocess.run(["open", url], check=False)
        return True

    THEMES = ("system", "light", "dark")

    def set_theme(self, theme: str) -> None:
        if theme not in self.THEMES:
            raise ValueError(f"theme must be one of {self.THEMES}")
        self.db.set_state("pref:theme", theme)
        self._bump()

    @property
    def theme(self) -> str:
        return self.db.get_state("pref:theme", "system") or "system"

    def touch(self) -> None:
        """Something changed on disk (a supplier rule); redraw."""
        self._bump()

    def _notification_status(self) -> str:
        if self.notifier is None:
            return "unavailable"
        self.notifier.refresh()
        return self.notifier.status

    # ---- what to notify about, and how often ------------------------------
    #
    # One switch turns everything off; each message also belongs to a
    # category you can switch off on its own. "instant" posts
    # it as it happens; "hourly" and "daily" hold messages back and post one
    # summary. Problems (a lapsed sign-in, a stuck inbox) are never held:
    # waiting a day to hear that nothing is arriving defeats the point.
    # Nor is a new version: it's posted once, when it's first seen.

    NOTIFY_CATEGORIES = ("new", "filed", "problems", "updates")
    NOTIFY_FREQUENCIES = ("instant", "hourly", "daily")
    NOTIFY_DAILY_HOUR = 9

    def notification_prefs(self) -> dict[str, Any]:
        freq = self.db.get_state("pref:notify_frequency", "instant")
        return {
            "enabled": self.db.get_state("pref:notify_enabled", "1") == "1",
            "frequency": freq if freq in self.NOTIFY_FREQUENCIES else "instant",
            "categories": {c: self.db.get_state(f"pref:notify_{c}", "1") == "1"
                           for c in self.NOTIFY_CATEGORIES},
            "waiting": len(self._pending_notifications()),
        }

    def set_notification_prefs(self, frequency: str | None = None,
                               categories: dict[str, bool] | None = None,
                               enabled: bool | None = None) -> None:
        if frequency is not None and frequency not in self.NOTIFY_FREQUENCIES:
            raise ValueError(f"frequency must be one of {self.NOTIFY_FREQUENCIES}")
        unknown = set(categories or {}) - set(self.NOTIFY_CATEGORIES)
        if unknown:
            raise ValueError(f"unknown notification type: {', '.join(sorted(unknown))}")
        if enabled is not None:
            self.db.set_state("pref:notify_enabled", "1" if enabled else "0")
            if not enabled:
                self.db.set_state("notify:pending", "[]")
        if frequency is not None:
            self.db.set_state("pref:notify_frequency", frequency)
        for category, on in (categories or {}).items():
            self.db.set_state(f"pref:notify_{category}", "1" if on else "0")
            if not on:
                self._drop_pending(category)
        if frequency == "instant" and self.notification_prefs()["enabled"]:
            self.flush_notifications(force=True)
        self._bump()

    def notify_about(self, category: str, message: str) -> None:
        """Post, hold for the next summary, or drop, per the preferences."""
        if self.notify is None:
            return
        prefs = self.notification_prefs()
        if not prefs["enabled"] or not prefs["categories"].get(category, True):
            return
        if category in ("problems", "updates") or prefs["frequency"] == "instant":
            self.notify("Receipt Bridge", message)
            return
        with self._lock:
            pending = self._pending_notifications()
            pending.append({"category": category, "message": message})
            self.db.set_state("notify:pending", json.dumps(pending))
        self._bump()

    def flush_notifications(self, force: bool = False) -> None:
        """Post the held messages as one, when the hour or the day is up."""
        if self.notify is None:
            return
        with self._lock:
            pending = self._pending_notifications()
            if not pending:
                return
            if not force and not self._summary_due(self.notification_prefs()["frequency"]):
                return
            self.db.set_state("notify:pending", "[]")
            self.db.set_state("notify:last_summary", _now())
        lines = [p["message"].removesuffix(" Ready to review.").rstrip(".") for p in pending]
        self.notify("Receipt Bridge", lines[0] + "." if len(lines) == 1
                    else f"{len(lines)} updates:\n" + "\n".join(lines))
        self._bump()

    def _summary_due(self, frequency: str) -> bool:
        last = self.db.get_state("notify:last_summary")
        try:
            last_at = datetime.fromisoformat(last) if last else None
        except ValueError:
            last_at = None
        now = datetime.now(timezone.utc)
        if frequency == "hourly":
            return last_at is None or now - last_at >= timedelta(hours=1)
        if frequency == "daily":
            local = datetime.now().astimezone()
            today_at = local.replace(hour=self.NOTIFY_DAILY_HOUR, minute=0, second=0, microsecond=0)
            return local >= today_at and (last_at is None or last_at < today_at)
        return True

    def _pending_notifications(self) -> list[dict[str, str]]:
        try:
            pending = json.loads(self.db.get_state("notify:pending", "[]") or "[]")
        except ValueError:
            return []
        return pending if isinstance(pending, list) else []

    def _drop_pending(self, category: str) -> None:
        with self._lock:
            kept = [p for p in self._pending_notifications() if p.get("category") != category]
            self.db.set_state("notify:pending", json.dumps(kept))

    def test_notification(self) -> bool:
        if self.notify is None:
            return False
        self.notify("Receipt Bridge", "Notifications are working.")
        return True

    def set_open_at_login(self, enabled: bool) -> None:
        """Turning it on without the app in /Applications installs it first,
        in the background: nothing to do by hand."""
        if enabled and login_item.bundle_path() is None:
            self._enqueue("install-app", self._run_install_and_open_at_login)
            return
        login_item.set_enabled(enabled)
        self._bump()

    def _run_install_and_open_at_login(self) -> None:
        self._set_activity(busy=True, kind="install", label="Installing Receipt Bridge", started_at=_now(), log=[])
        try:
            login_item.install_app()
            login_item.set_enabled(True)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            log.warning("install failed: %s", exc)
            self._finish("install", False, f"Couldn't install Receipt Bridge: {exc}")
            return
        self._finish("install", True, "Installed in Applications. It will open when you log in.")

    def set_auto_scan(self, enabled: bool) -> None:
        self.db.set_state("pref:auto_scan", "1" if enabled else "0")
        self._bump()

    def all_suppliers(self) -> tuple[list[Any], list[str]]:
        """Every rule, switched-off ones included.

        The scan only wants enabled rules, but Settings must list the rest
        too — otherwise switching a supplier off made it vanish, with no way
        to switch it back on.
        """
        from .watchers import WatcherError, load_watchers

        def real(watchers):
            # "_name.yaml" files are templates (watchers/_example.yaml), not
            # suppliers; listing them once disabled rules became visible put a
            # phantom "Example Supplier" in Settings.
            return [w for w in watchers if not (w.path and w.path.name.startswith("_"))]

        try:
            return real(load_watchers(self.config.watchers_dir, include_disabled=True)), []
        except WatcherError as exc:
            enabled, problems = load_watchers_safe(self.config.watchers_dir)
            return real(enabled), problems or [str(exc)]

    def rescan_supplier(self, watcher_id: str) -> None:
        """Look through a rule's whole window again, from the date chosen
        when each account was connected. Scans otherwise resume where the
        last one stopped, so a rule added again under an old id, or edited to
        match different emails, only ever saw the last few days."""
        for key in self.db.list_state("last_scan:"):
            if key == f"last_scan:{watcher_id}" or key.endswith(f":{watcher_id}"):
                self.db.delete_state(key)
        self.scan("supplier changed")

    def supplier_path(self, watcher_id: str) -> Path | None:
        watchers, _ = self.all_suppliers()
        found = next((w for w in watchers if w.id == watcher_id), None)
        return found.path if found else None

    def suppliers(self) -> list[dict[str, Any]]:
        watchers, problems = self.all_suppliers()
        found: dict[str, int] = {}
        for row in self.db.list_receipts():
            found[row["watcher_id"]] = found.get(row["watcher_id"], 0) + 1
        result = [
            {
                "id": w.id,
                "name": w.name,
                "receipts": found.get(w.id, 0),
                "gets_supplier_document": _has_better_source(w),
                "file": w.path.name if w.path else "",
                "enabled": w.enabled,
            }
            for w in watchers
        ]
        return result + [{"problem": p} for p in problems]


def _has_better_source(watcher: Any) -> bool:
    return any(
        isinstance(step, dict) and (step.get("fetcher") or step.get("step") == "attachment")
        or step == "attachment"
        for step in watcher.pdf
    )


# A row as the UI wants it: plain types, and the document's provenance in
# words rather than internal source codes:
#   supplier  — the supplier's own receipt (downloaded or attached)
#   email     — the email itself, and that is all this supplier provides
#   fallback  — the email, because getting the supplier's receipt failed
#   photo     — photographed on the phone and read on this Mac
def _receipt_json(row: Any, better: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    source = row["pdf_source"] or ""
    keys = row.keys()
    extra: dict[str, Any] = {}
    if "extra_json" in keys and row["extra_json"]:
        try:
            extra = json.loads(row["extra_json"])
        except ValueError:
            extra = {}
    is_photo = "source" in keys and row["source"] == "photo"
    if is_photo:
        document = "photo"
    elif not source:
        document = "none"
    elif source == "rendered_email":
        document = "fallback" if row["watcher_id"] in better else "email"
    else:
        document = "supplier"
    return {
        "id": row["id"],
        "supplier": row["vendor"] or row["watcher_id"],
        "watcher": row["watcher_id"],
        "date": row["purchased_on"] or row["email_date"],
        "total": row["total"],
        "currency": row["currency"] or "",
        "description": row["description"] or row["subject"] or "",
        "reference": row["reference"] or "",
        "filename": row["filename"] or "",
        "document": document,
        "has_pdf": bool(row["pdf_path"]),
        "status": row["status"],
        "error": row["error"] or "",
        "account": row["account"] or "",
        "exported_at": row["exported_at"],
        "export_folder": Path(row["export_path"]).parent.name if row["export_path"] else "",
        "source": row["source"] if "source" in keys else "gmail",
        "paid_by": row["paid_by"] if "paid_by" in keys else None,
        "vat": row["vat"] if "vat" in keys else None,
        "vat_number": (row["vat_number"] if "vat_number" in keys else None) or "",
        "total_status": (row["total_status"] if "total_status" in keys else None) or "",
        "flags": extra.get("flags", []) if is_photo else [],
        "is_image": bool(row["pdf_path"]) and Path(row["pdf_path"]).suffix.lower() in IMAGE_TYPES,
    }


_LEGAL_WORDS = re.compile(r"\b(?:ltd|limited|plc|inc|llc|llp|gmbh|co|company|uk)\b\.?", re.IGNORECASE)


def _supplier_key_for_names(name: str) -> str:
    """What makes two supplier names the same supplier, for suggestions:
    "Boots Ltd", "BOOTS" and "Boot" all come to "boot"."""
    key = re.sub(r"[^a-z0-9]+", "", _LEGAL_WORDS.sub(" ", name.lower().replace("&", " ")))
    return key[:-1] if len(key) > 3 and key.endswith("s") else key or name.lower()


def _email_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """The Emails view's form, checked: a mistake is a ValueError in words."""
    supplier = " ".join(str(fields.get("supplier") or "").split())[:80]
    day = str(fields.get("date") or "").strip()[:10]
    if day:
        try:
            date.fromisoformat(day)
        except ValueError:
            raise ValueError("The date should look like 2026-10-08.") from None

    def amount(key: str, name: str) -> float | None:
        value = fields.get(key)
        if value in (None, ""):
            return None
        try:
            number = round(float(str(value).replace(",", "").replace("£", "").strip()), 2)
        except ValueError:
            raise ValueError(f"The {name} should be a number.") from None
        if number < 0:
            raise ValueError(f"The {name} can't be negative.")
        return number

    from .filer import AMOUNT, AUTO, VAT_CHOICES

    total, vat = amount("total", "total"), amount("vat", "VAT")
    if vat is not None and total is not None and vat >= total:
        raise ValueError("The VAT should be less than the total.")
    # FreeAgent's VAT menu, as in Files: Auto (as printed), Amount…, 20%, 5%,
    # 0%, Exempt or Out of Scope. The figure only matters for Auto and Amount.
    vat_choice = str(fields.get("vat_choice") or AUTO)
    if vat_choice not in VAT_CHOICES:
        raise ValueError("Choose a VAT rate from the list.")
    if vat_choice not in (AUTO, AMOUNT):
        vat = None
    elif vat_choice == AMOUNT and vat and total and vat > round(total * 20 / 120, 2) + 0.01:
        raise ValueError(f"£{vat:.2f} is more than 20% VAT on £{total:.2f}.")
    currency = str(fields.get("currency") or "GBP").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("The currency should be three letters, like GBP.")
    paid_by = fields.get("paid_by") or "business"
    if paid_by not in ("business", "personal"):
        raise ValueError("Paid by should be business or personal.")
    return {"supplier": supplier, "date": day, "total": total, "vat": vat, "vat_choice": vat_choice,
            "currency": currency, "paid_by": paid_by}


def _money_text(value: Any, currency: str | None) -> str:
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency or "GBP", "")
    amount = f"{float(value):,.2f}"
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}"


def _day_text(iso: str | None) -> str:
    try:
        day = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return "an unknown date"
    return f"{day.day} {day:%b}"


def _a_or_an(word: str) -> str:
    return "an" if (word or "x")[0].lower() in "aeiou" else "a"
