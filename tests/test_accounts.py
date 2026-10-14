"""Gmail account store tests. No network, no real tokens.

Token files are fabricated on disk: everything here is about how accounts are
listed, described and removed, and about not losing scan history when the
state keys changed shape.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.accounts import AccountStore, _slug, adopt_legacy_scan_state  # noqa: E402
from app.db import Database  # noqa: E402

READONLY = "https://www.googleapis.com/auth/gmail.readonly"
MODIFY = "https://www.googleapis.com/auth/gmail.modify"


def _config(tmp: Path) -> SimpleNamespace:
    return SimpleNamespace(
        accounts_dir=tmp / "accounts",
        token_file=tmp / "token.json",
        credentials_file=tmp / "credentials.json",
        scopes=[READONLY],
    )


def _write_token(store: AccountStore, email: str, scopes=None, refresh="r-token"):
    path = store.dir / f"{_slug(email)}.json"
    payload = {"token": "a-token", "scopes": scopes or [READONLY]}
    if refresh:
        payload["refresh_token"] = refresh
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_new_mailbox_starts_from_the_date_you_chose() -> None:
    """Connecting asks how far back to look: the first scan starts there;
    later scans carry on from the last one."""
    from datetime import date
    from app.gmail_client import with_date_window
    q = "from:receipts@example.com"
    assert with_date_window(q, None, 90, date(2025, 1, 15)).endswith("after:2025/01/15")
    assert with_date_window(q, date(2026, 3, 10), 90, date(2025, 1, 15)).endswith("after:2026/03/07")
    assert "after:" in with_date_window(q, None, 90)


def test_slug_makes_addresses_safe_but_readable() -> None:
    """Readable in Finder, and incapable of escaping the accounts folder.

    Dots survive (they belong in addresses); separators do not, which is what
    actually prevents a crafted address writing outside the directory.
    """
    assert _slug("Jo.Bloggs@Gmail.com") == "jo.bloggs@gmail.com"

    import os

    hostile = "../../etc/passwd@example.com"
    slug = _slug(hostile)
    assert "/" not in slug and os.sep not in slug

    with tempfile.TemporaryDirectory() as tmp:
        accounts_dir = Path(tmp) / "accounts"
        accounts_dir.mkdir()
        target = (accounts_dir / f"{slug}.json").resolve()
        assert target.parent == accounts_dir.resolve(), (
            "a crafted address must not write outside the accounts folder"
        )


def test_lists_connected_accounts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(_config(Path(tmp)))
        assert store.list() == []

        _write_token(store, "one@example.com")
        _write_token(store, "two@example.com")

        assert [a.email for a in store.list()] == [
            "one@example.com",
            "two@example.com",
        ]


def test_reports_read_only_scope() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(_config(Path(tmp)))
        _write_token(store, "safe@example.com")
        _write_token(store, "risky@example.com", scopes=[READONLY, MODIFY])

        by_email = {a.email: a for a in store.list()}
        assert by_email["safe@example.com"].read_only
        assert not by_email["risky@example.com"].read_only, (
            "a write scope must be surfaced, not hidden"
        )



def test_only_a_modify_grant_can_label() -> None:
    """Accounts connected before labelling are read-only and are left
    unlabelled until reconnected."""
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(_config(Path(tmp)))
        _write_token(store, "old@example.com")
        _write_token(store, "new@example.com", scopes=[READONLY, MODIFY])

        by_email = {a.email: a for a in store.list()}
        assert not by_email["old@example.com"].can_label
        assert by_email["new@example.com"].can_label


def test_sign_in_asks_to_label_unless_turned_off() -> None:
    """An older config.yaml lists read-only alone; sign-in still asks for
    labelling, unless `label_filed` is off."""
    from app.config import Config

    old = Config(raw={"gmail": {"scopes": [READONLY]}})
    assert old.scopes == [READONLY, MODIFY]
    off = Config(raw={"gmail": {"scopes": [READONLY], "label_filed": False}})
    assert off.scopes == [READONLY]


class _Call:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _FakeGmail:
    """Just the labels and messages.modify parts of the Gmail API."""

    def __init__(self, labels):
        self.labels_in_mailbox = [{"id": f"L{n}", "name": name} for n, name in enumerate(labels)]
        self.lists = 0
        self.modified = []

    def users(self):
        return self

    def labels(self):
        return self

    def messages(self):
        return self

    def list(self, userId):
        self.lists += 1
        return _Call({"labels": list(self.labels_in_mailbox)})

    def create(self, userId, body):
        label = {"id": f"L{len(self.labels_in_mailbox)}", "name": body["name"]}
        self.labels_in_mailbox.append(label)
        return _Call(label)

    def modify(self, userId, id, body):
        self.modified.append((id, body))
        return _Call({})


def _client_with(gmail):
    from app.gmail_client import GmailClient

    client = GmailClient(Path("/nonexistent/credentials.json"), Path("/nonexistent/token.json"))
    client._service = gmail
    return client


def test_a_label_is_made_inside_its_parent_once() -> None:
    gmail = _FakeGmail(["INBOX"])
    client = _client_with(gmail)
    client.relabel("m1", add=["Receipt Bridge/Filed as Expense"])
    client.relabel("m2", add=["receipt bridge/filed as expense"])   # Gmail ignores case

    names = [label["name"] for label in gmail.labels_in_mailbox]
    assert names == ["INBOX", "Receipt Bridge", "Receipt Bridge/Filed as Expense"]
    assert gmail.lists == 1, "the mailbox's labels are listed once per client"
    assert gmail.modified == [("m1", {"addLabelIds": ["L2"], "removeLabelIds": []}),
                              ("m2", {"addLabelIds": ["L2"], "removeLabelIds": []})]


def test_taking_off_a_label_never_made_does_nothing() -> None:
    gmail = _FakeGmail(["INBOX"])
    _client_with(gmail).relabel("m1", remove=["Receipt Bridge/Filed as Expense"])
    assert gmail.modified == []
    assert len(gmail.labels_in_mailbox) == 1


def _labelling_service(tmp: Path, scopes):
    """Enough of ReceiptService for _label_emails, with one fake mailbox."""
    from app.config import Config

    db = Database(tmp / "receipts.sqlite3")
    store = AccountStore(_config(tmp))
    _write_token(store, "me@example.com", scopes=scopes)
    gmail = _FakeGmail([])
    notes = []
    service = SimpleNamespace(config=Config(raw={}), accounts=store, db=db, _note=notes.append)

    def client(self, credentials_file, scopes):
        return _client_with(gmail)

    from app.accounts import Account
    original = Account.client
    Account.client = client
    return service, db, gmail, notes, lambda: setattr(Account, "client", original)


def test_a_filed_email_is_labelled_by_how_it_was_filed() -> None:
    from app.db import FILED, PENDING
    from app.service import BANK_LABEL, EXPENSE_LABEL, ReceiptService

    with tempfile.TemporaryDirectory() as tmp:
        service, db, gmail, notes, restore = _labelling_service(Path(tmp), [READONLY, MODIFY])
        try:
            base = {"watcher_id": "email", "vendor": "Shop", "filename": "r.pdf",
                    "total": 5.0, "currency": "GBP", "account": "me@example.com"}
            expense = db.insert_receipt({**base, "gmail_message_id": "m1"})
            bank = db.insert_receipt({**base, "gmail_message_id": "m2"})
            photo = db.insert_receipt({**base, "source": "photo", "source_id": "sha256:x"})
            db.update_receipt(expense, {"status": FILED, "freeagent_json": '{"kind": "expense"}'})
            db.update_receipt(bank, {"status": FILED, "freeagent_json": '{"kind": "explanation"}'})

            ReceiptService._label_emails(service, [expense, bank, photo])
            ids = {label["name"]: label["id"] for label in gmail.labels_in_mailbox}
            assert gmail.modified == [
                ("m1", {"addLabelIds": [ids[EXPENSE_LABEL]], "removeLabelIds": []}),
                ("m2", {"addLabelIds": [ids[BANK_LABEL]], "removeLabelIds": [ids[EXPENSE_LABEL]]}),
            ]

            # Undo: back in To file, so the label comes off
            gmail.modified.clear()
            db.update_receipt(expense, {"status": PENDING})
            ReceiptService._label_emails(service, [expense])
            assert gmail.modified == [
                ("m1", {"addLabelIds": [], "removeLabelIds": [ids[EXPENSE_LABEL], ids[BANK_LABEL]]}),
            ]
            assert notes == []
        finally:
            restore()


def test_a_read_only_mailbox_is_not_labelled() -> None:
    from app.db import FILED
    from app.service import ReceiptService

    with tempfile.TemporaryDirectory() as tmp:
        service, db, gmail, notes, restore = _labelling_service(Path(tmp), [READONLY])
        try:
            receipt = db.insert_receipt({"watcher_id": "email", "vendor": "Shop", "filename": "r.pdf",
                                         "gmail_message_id": "m1", "account": "me@example.com"})
            db.update_receipt(receipt, {"status": FILED, "freeagent_json": '{"kind": "expense"}'})
            ReceiptService._label_emails(service, [receipt])
            assert gmail.modified == [] and gmail.lists == 0
        finally:
            restore()


def test_flags_a_token_with_no_refresh_token() -> None:
    """Without one the account silently stops working within the hour."""
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(_config(Path(tmp)))
        _write_token(store, "good@example.com")
        _write_token(store, "stale@example.com", refresh=None)

        by_email = {a.email: a for a in store.list()}
        assert by_email["good@example.com"].has_refresh_token
        assert not by_email["stale@example.com"].has_refresh_token


def test_removing_an_account_deletes_its_token() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(_config(Path(tmp)))
        path = _write_token(store, "one@example.com")

        # revoke=False keeps the test off the network.
        assert store.remove("one@example.com", revoke=False)
        assert not path.exists()
        assert store.list() == []
        assert not store.remove("gone@example.com", revoke=False)


def test_scan_positions_survive_the_move_to_per_account_keys() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        store = AccountStore(_config(tmp_path))
        _write_token(store, "one@example.com")
        db = Database(tmp_path / "receipts.sqlite3")
        db.set_state("last_scan:trainline", "2026-09-01")
        db.set_state("last_scan:yesim", "2026-08-30")

        assert adopt_legacy_scan_state(db, store) == 2
        assert db.get_state("last_scan:one@example.com:trainline") == "2026-09-01"
        assert db.get_state("last_scan:one@example.com:yesim") == "2026-08-30"


def test_scan_positions_are_left_alone_when_ambiguous() -> None:
    """With two mailboxes there is no telling which the old position was."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        store = AccountStore(_config(tmp_path))
        _write_token(store, "one@example.com")
        _write_token(store, "two@example.com")
        db = Database(tmp_path / "receipts.sqlite3")
        db.set_state("last_scan:trainline", "2026-09-01")

        assert adopt_legacy_scan_state(db, store) == 0


def test_migration_is_not_applied_twice() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        store = AccountStore(_config(tmp_path))
        _write_token(store, "one@example.com")
        db = Database(tmp_path / "receipts.sqlite3")
        db.set_state("last_scan:trainline", "2026-09-01")

        assert adopt_legacy_scan_state(db, store) == 1
        # Already-scoped keys must not be re-scoped into nonsense.
        assert adopt_legacy_scan_state(db, store) == 0, "migrated twice"
        assert not db.get_state("last_scan:one@example.com:one@example.com:trainline")
        assert db.get_state("last_scan:trainline") is None, "legacy key kept"


def test_migration_never_rewinds_a_newer_position() -> None:
    """The bug: every launch copied the old position over the current one."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        store = AccountStore(_config(tmp_path))
        _write_token(store, "one@example.com")
        db = Database(tmp_path / "receipts.sqlite3")
        db.set_state("last_scan:one@example.com:trainline", "2026-10-04")
        db.set_state("last_scan:trainline", "2026-09-05")

        adopt_legacy_scan_state(db, store)
        assert db.get_state("last_scan:one@example.com:trainline") == "2026-10-04"


def test_a_missing_token_reads_as_signed_out() -> None:
    """`check` must classify, never raise — it runs on every page load."""
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(_config(Path(tmp)))
        _write_token(store, "one@example.com")
        account = store.list()[0]
        account.token_path.unlink()

        state, _ = account.check(Path(tmp) / "credentials.json", [READONLY])
        assert state == "signed_out"


def test_an_unreadable_token_does_not_raise() -> None:
    """A corrupt token must degrade to a status, not a 500 on the page."""
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(_config(Path(tmp)))
        path = store.dir / "broken@example.com.json"
        path.write_text("not json at all", encoding="utf-8")

        state, detail = store.list()[0].check(
            Path(tmp) / "credentials.json", [READONLY]
        )
        assert state in {"expired", "error"}, state
        assert detail


def test_existing_token_file_is_not_treated_as_working() -> None:
    """The bug this guards: a revoked account looked healthy on the status
    page because only the file's existence was ever checked."""
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(_config(Path(tmp)))
        _write_token(store, "one@example.com")
        account = store.list()[0]
        client = account.client(Path(tmp) / "credentials.json", [READONLY])

        assert client.is_authorised(), "the file exists"
        state, _ = account.check(Path(tmp) / "credentials.json", [READONLY])
        assert state != "ok", (
            "a fabricated token must never report as working"
        )


def test_receipts_are_counted_per_mailbox() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "receipts.sqlite3")
        base = {
            "watcher_id": "yesim",
            "vendor": "Yesim",
            "filename": "r.pdf",
            "total": 8.64,
            "currency": "GBP",
        }
        db.insert_receipt({**base, "gmail_message_id": "m1", "account": "a@x.com"})
        db.insert_receipt({**base, "gmail_message_id": "m2", "account": "a@x.com"})
        db.insert_receipt({**base, "gmail_message_id": "m3", "account": "b@x.com"})
        # Staged before accounts existed: NULL, counted as unattributed.
        db.insert_receipt({**base, "gmail_message_id": "m4"})

        assert db.count_for_account("a@x.com") == 2
        assert db.count_for_account("b@x.com") == 1
        assert db.count_for_account("") == 1


def test_a_sign_in_waiting_on_the_browser_can_be_cancelled() -> None:
    """Abandoning the Google tab used to lock "Connect Gmail" for five
    minutes. Cancel stops the real flow straight away and leaves nothing
    behind."""
    import threading
    import time
    from unittest import mock

    from app.gmail_client import SignInCancelled

    with tempfile.TemporaryDirectory() as tmp:
        config = _config(Path(tmp))
        config.credentials_file.write_text(json.dumps({"installed": {
            "client_id": "id", "client_secret": "secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }}), encoding="utf-8")
        store = AccountStore(config)
        assert not store.cancel_interactive(), "nothing to cancel yet"

        outcome = {}

        def run() -> None:
            try:
                store.add_interactive()
            except Exception as exc:
                outcome["error"] = exc

        with mock.patch("webbrowser.get"):          # no real browser tab
            thread = threading.Thread(target=run)
            thread.start()
            for _ in range(100):
                if store.cancel_interactive():
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("sign-in never started")
            thread.join(timeout=10)

        assert not thread.is_alive(), "cancel must not wait out the timeout"
        assert isinstance(outcome.get("error"), SignInCancelled)
        assert store.list() == []
        assert not (store.dir / ".pending.json").exists()


def test_signing_in_always_asks_which_google_account() -> None:
    """With one Google session in the browser, Google skipped its chooser and
    "Add account" signed the same mailbox in again, so a second one could
    never be added. Reconnecting pre-selects the address it is for."""
    from unittest import mock

    from google_auth_oauthlib.flow import InstalledAppFlow

    from app.gmail_client import SignInCancelled

    with tempfile.TemporaryDirectory() as tmp:
        config = _config(Path(tmp))
        config.credentials_file.write_text(json.dumps({"installed": {
            "client_id": "id", "client_secret": "secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }}), encoding="utf-8")
        store = AccountStore(config)
        asked = []

        def run_local_server(self, **kwargs):
            asked.append(kwargs)
            raise SignInCancelled("stop here")

        with mock.patch.object(InstalledAppFlow, "run_local_server", run_local_server):
            for hint in ("", "b@x.com"):
                try:
                    store.add_interactive(hint)
                except SignInCancelled:
                    pass

        assert all("select_account" in a["prompt"].split() for a in asked)
        assert all("consent" in a["prompt"].split() for a in asked), "consent still gets a refresh token"
        assert "login_hint" not in asked[0]
        assert asked[1]["login_hint"] == "b@x.com"


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

