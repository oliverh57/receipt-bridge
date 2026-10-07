"""Gmail accounts connected to Receipt Bridge.

Receipts arrive at whichever mailbox happens to be on the booking, so the app
supports several accounts at once — a personal address and a business one, say
— and scans each of them with every watcher.

One token file per account under `data/accounts/`, named after the address it
belongs to. Each is a long-lived key to a mailbox, so they are written
owner-only, and signing out revokes the grant with Google rather than merely
deleting the local copy.

Every account is read-only (`gmail.readonly`). Nothing here can send, delete
or label anything.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .gmail_client import READONLY_SCOPE, GmailAuthError, GmailClient

log = logging.getLogger(__name__)

REVOKE_URL = "https://oauth2.googleapis.com/revoke"
PERMISSIONS_URL = "https://myaccount.google.com/permissions"


def _slug(email: str) -> str:
    """A filename-safe form of an address, still readable in Finder."""
    return re.sub(r"[^A-Za-z0-9._@-]", "_", email.strip().lower())


@dataclass
class Account:
    email: str
    token_path: Path

    @property
    def added_at(self) -> str | None:
        try:
            stamp = self.token_path.stat().st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat(
            timespec="seconds"
        )

    @property
    def scopes(self) -> list[str]:
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return list(data.get("scopes") or [])

    @property
    def read_only(self) -> bool:
        """True when the grant cannot modify the mailbox."""
        return all(scope.endswith("gmail.readonly") for scope in self.scopes)

    @property
    def has_refresh_token(self) -> bool:
        """Without one, the grant dies when the access token expires."""
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return bool(data.get("refresh_token"))

    def client(self, credentials_file: Path, scopes: list[str]) -> GmailClient:
        return GmailClient(credentials_file, self.token_path, scopes)

    def check(self, credentials_file: Path, scopes: list[str]) -> tuple[str, str]:
        """Live check of whether this account still works. (state, detail)."""
        return self.client(credentials_file, scopes).check()


class AccountStore:
    def __init__(self, config):
        self.config = config
        self.dir = config.accounts_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._pending: GmailClient | None = None   # a sign-in waiting on the browser
        self._migrate_legacy_token()

    # ---- listing --------------------------------------------------------

    def list(self) -> list[Account]:
        accounts = [
            Account(email=path.stem, token_path=path)
            for path in sorted(self.dir.glob("*.json"))
        ]
        return accounts

    def get(self, email: str) -> Account | None:
        path = self.dir / f"{_slug(email)}.json"
        return Account(email=path.stem, token_path=path) if path.exists() else None

    def clients(self) -> list[tuple[Account, GmailClient]]:
        return [
            (a, a.client(self.config.credentials_file, self.config.scopes))
            for a in self.list()
        ]

    # ---- adding and removing --------------------------------------------

    def cancel_interactive(self) -> bool:
        """Stop the sign-in add_interactive is waiting on, if there is one."""
        client = self._pending
        return bool(client and client.cancel_sign_in())

    def add_interactive(self) -> Account:
        """Run Google's sign-in flow and store the result under its address.

        The flow deliberately opens the user's real browser. Google refuses
        OAuth inside embedded webviews, so this cannot happen in the app's own
        window even though everything else does.
        """
        staging = self.dir / ".pending.json"
        staging.unlink(missing_ok=True)

        client = GmailClient(
            self.config.credentials_file, staging, self.config.scopes
        )
        self._pending = client
        try:
            email = client.authorise()
        except Exception:
            staging.unlink(missing_ok=True)
            raise
        finally:
            self._pending = None

        if not email:
            staging.unlink(missing_ok=True)
            raise GmailAuthError("Signed in, but Gmail did not return an address.")

        final = self.dir / f"{_slug(email)}.json"
        staging.replace(final)
        final.chmod(0o600)
        log.info("connected %s", email)
        return Account(email=final.stem, token_path=final)

    def remove(self, email: str, revoke: bool = True) -> bool:
        """Sign an account out. Revokes the grant with Google by default."""
        account = self.get(email)
        if account is None:
            return False

        if revoke:
            self._revoke(account)
        account.token_path.unlink(missing_ok=True)
        log.info("disconnected %s", account.email)
        return True

    def _revoke(self, account: Account) -> None:
        """Tell Google to forget the grant.

        Deleting the local token only makes this app stop using it; the
        permission would still be listed in the user's Google account. Best
        effort — if the network is down the file is still removed.
        """
        import urllib.error
        import urllib.parse
        import urllib.request

        try:
            data = json.loads(account.token_path.read_text(encoding="utf-8"))
        except Exception:
            return
        token = data.get("refresh_token") or data.get("token")
        if not token:
            return

        try:
            request = urllib.request.Request(
                REVOKE_URL,
                data=urllib.parse.urlencode({"token": token}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            urllib.request.urlopen(request, timeout=10).read()
        except Exception as exc:
            log.warning("could not revoke access for %s: %s", account.email, exc)

    # ---- compatibility ---------------------------------------------------

    def _migrate_legacy_token(self) -> None:
        """Adopt the single `data/token.json` from before multi-account."""
        legacy = self.config.token_file
        if not legacy.exists() or any(self.dir.glob("*.json")):
            return

        try:
            client = GmailClient(
                self.config.credentials_file, legacy, self.config.scopes
            )
            email = client.profile_address()
        except Exception as exc:
            log.warning("could not identify the existing token: %s", exc)
            return

        if not email:
            return
        target = self.dir / f"{_slug(email)}.json"
        target.write_bytes(legacy.read_bytes())
        target.chmod(0o600)
        legacy.unlink(missing_ok=True)
        log.info("moved existing sign-in to %s", target.name)


def adopt_legacy_scan_state(db, store: AccountStore) -> int:
    """Re-key scan positions recorded before accounts were tracked.

    Scan progress used to be stored as `last_scan:<watcher>`; it is now
    `last_scan:<address>:<watcher>`. Without moving them, every watcher looks
    unscanned and the next run re-walks its whole lookback window — harmless
    thanks to deduplication, but it would mean re-fetching dozens of receipts
    through a browser for nothing.

    Only safe when exactly one account is connected: with several, there is no
    way to know which mailbox the old position belonged to.
    """
    connected = store.list()
    if len(connected) != 1:
        return 0

    email = connected[0].email
    moved = 0
    for key, value in db.list_state("last_scan:").items():
        remainder = key.split(":", 1)[1]
        if ":" in remainder:
            continue  # already account-scoped
        scoped = f"last_scan:{email}:{remainder}"
        # Never overwrite a newer position. The first version of this copied
        # unconditionally and kept the old key, so every launch rewound each
        # supplier to the day multi-account support arrived and re-walked a
        # month of mail.
        if db.get_state(scoped) is None:
            db.set_state(scoped, value)
            moved += 1
        db.delete_state(key)
    if moved:
        log.info("carried %d scan position(s) over to %s", moved, email)
    return moved


def default_scopes() -> list[str]:
    return [READONLY_SCOPE]
