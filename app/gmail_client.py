"""Read-only Gmail access.

Scope is `gmail.readonly` and nothing else: this app can search and read, and
is structurally incapable of sending, deleting or labelling anything in the
mailbox. The refresh token is cached locally so sign-in happens once.
"""

from __future__ import annotations

import logging
import socket
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .email_message import Email

log = logging.getLogger(__name__)

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# Seconds before any single Gmail API call gives up.
REQUEST_TIMEOUT = 20

# Seconds to wait for the user to finish signing in to Google.
SIGN_IN_TIMEOUT = 300


class GmailAuthError(RuntimeError):
    """Credentials are missing or the user has not signed in yet."""


class SignInCancelled(GmailAuthError):
    """The user cancelled a sign-in that was waiting on the browser."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


class GmailClient:
    def __init__(
        self,
        credentials_file: Path,
        token_file: Path,
        scopes: list[str] | None = None,
    ):
        self.credentials_file = Path(credentials_file)
        self.token_file = Path(token_file)
        self.scopes = scopes or [READONLY_SCOPE]
        self._service = None
        self._sign_in_port: int | None = None
        self._cancelled = False

    # ---- auth -----------------------------------------------------------

    def is_authorised(self) -> bool:
        """A token file exists. Says nothing about whether it still works."""
        return self.token_file.exists()

    def check(self) -> tuple[str, str]:
        """Actually exercise the credentials. Returns (state, detail).

        States: `ok`, `signed_out`, `expired`, `error`.

        `is_authorised` only proves a file is on disk, which is why a revoked
        account could sit on the status screen looking healthy until a scan
        failed. Google expires refresh tokens after 7 days while the OAuth
        consent screen is in Testing, so this is a routine state, not an edge
        case, and the UI needs to be able to show it.
        """
        if not self.token_file.exists():
            return "signed_out", "No token stored."

        try:
            self._service = None
            self._load_credentials(interactive=False)
            address = self.profile_address()
            return "ok", address
        except GmailAuthError:
            return "expired", "Sign-in has expired — reconnect this account."
        except Exception as exc:
            text = str(exc)
            if "invalid_grant" in text or "expired or revoked" in text:
                return "expired", "Google has expired or revoked this token."
            return "error", text[:200]

    def _load_credentials(self, interactive: bool):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if self.token_file.exists():
            creds = Credentials.from_authorized_user_file(
                str(self.token_file), self.scopes
            )

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save(creds)
                return creds
            except Exception as exc:
                log.warning("token refresh failed, re-authorising: %s", exc)

        if not interactive:
            raise GmailAuthError(
                "Not signed in to Gmail. Run: python cli.py auth"
            )

        if not self.credentials_file.exists():
            raise GmailAuthError(
                f"OAuth client secret not found at {self.credentials_file}. "
                "Create a Desktop app OAuth client in Google Cloud Console, "
                "download the JSON, and save it there."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_file), self.scopes
        )
        # Chosen here rather than by the flow so cancel_sign_in knows where
        # the flow is listening.
        self._sign_in_port = _free_port()
        self._cancelled = False
        try:
            creds = flow.run_local_server(
                port=self._sign_in_port,
                prompt="consent",
                # Without a limit, abandoning the Google tab left this waiting
                # forever, holding whatever thread called it.
                timeout_seconds=SIGN_IN_TIMEOUT,
                success_message=(
                    "Receipt Bridge is connected. You can close this tab and go "
                    "back to the app."
                ),
            )
        except Exception:
            if self._cancelled:
                raise SignInCancelled("Sign-in cancelled.") from None
            raise
        finally:
            self._sign_in_port = None
        if self._cancelled:
            raise SignInCancelled("Sign-in cancelled.")
        if not creds or not getattr(creds, "token", None):
            raise GmailAuthError("Sign-in was not completed.")
        self._save(creds)
        return creds

    def cancel_sign_in(self) -> bool:
        """Stop a sign-in that is waiting on the browser, from another thread.

        The flow blocks until one request reaches its local server, so send
        it one: an OAuth error redirect, which makes the flow raise instead
        of waiting out SIGN_IN_TIMEOUT.
        """
        port = self._sign_in_port
        if port is None:
            return False
        self._cancelled = True
        try:
            urllib.request.urlopen(
                f"http://localhost:{port}/?error=access_denied", timeout=5
            ).close()
        except Exception as exc:              # the flow may already be gone
            log.info("cancel sign-in: %s", exc)
        return True

    def _save(self, creds) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(creds.to_json(), encoding="utf-8")
        # The refresh token is a long-lived mailbox key; keep it owner-only.
        self.token_file.chmod(0o600)

    def authorise(self) -> str:
        """Run the interactive sign-in. Returns the signed-in address."""
        self._service = None
        self._load_credentials(interactive=True)
        return self.profile_address()

    def service(self, interactive: bool = False):
        if self._service is None:
            import google_auth_httplib2
            import httplib2
            from googleapiclient.discovery import build

            creds = self._load_credentials(interactive=interactive)
            # httplib2 waits forever by default. On a flaky connection that
            # froze whatever called it — the review screen included — so
            # every request now gives up and reports instead of hanging.
            http = google_auth_httplib2.AuthorizedHttp(
                creds, http=httplib2.Http(timeout=REQUEST_TIMEOUT)
            )
            self._service = build("gmail", "v1", http=http, cache_discovery=False)
        return self._service

    def profile_address(self) -> str:
        profile = (
            self.service().users().getProfile(userId="me").execute()
        )
        return str(profile.get("emailAddress", ""))

    # ---- reading --------------------------------------------------------

    def search(self, query: str, max_results: int = 100) -> list[dict]:
        """Return message id/thread id pairs matching a Gmail search query."""
        service = self.service()
        collected: list[dict] = []
        page_token = None

        while len(collected) < max_results:
            response = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=min(100, max_results - len(collected)),
                    pageToken=page_token,
                )
                .execute()
            )
            collected.extend(response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return collected[:max_results]

    def search_page(self, query: str, page_token: str = "",
                    max_results: int = 50) -> tuple[list[dict], str]:
        """One page of a search, and the token for the next ("" at the end).
        For a list read a page at a time, where `search` would walk on."""
        response = (
            self.service()
            .users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results, pageToken=page_token or None)
            .execute()
        )
        return response.get("messages", []), response.get("nextPageToken", "")

    def headers(self, message_ids: list[str]) -> list[dict]:
        """Subject, sender, date and snippet for several messages, cheaply.

        Uses Gmail's metadata format and one batched HTTP request, so listing
        fifteen search results costs one round trip instead of downloading
        fifteen whole emails.
        """
        service = self.service()
        found: dict[str, dict] = {}

        def collect(request_id, response, exception):
            if exception is not None or not response:
                return
            payload = response.get("payload") or {}
            heads = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            found[response["id"]] = {
                "id": response["id"],
                "thread_id": response.get("threadId", ""),
                "subject": heads.get("subject", ""),
                "sender": heads.get("from", ""),
                "date": heads.get("date", ""),
                "snippet": response.get("snippet", ""),
                "label_ids": response.get("labelIds", []),
                # when Gmail received it, as ISO: the Date header is whatever
                # the sender's server wrote, in any format
                "internal_date": _iso_from_ms(response.get("internalDate")),
                # metadata has no parts, but mail with attachments is "mixed"
                "has_attachment": payload.get("mimeType", "") == "multipart/mixed",
            }

        batch = service.new_batch_http_request(callback=collect)
        for message_id in message_ids:
            batch.add(
                service.users().messages().get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
            )
        batch.execute()
        return [found[m] for m in message_ids if m in found]

    def fetch(self, message_id: str, thread_id: str = "") -> Email:
        """Download one message in full and normalise it."""
        import base64

        raw = (
            self.service()
            .users()
            .messages()
            .get(userId="me", id=message_id, format="raw")
            .execute()
        )
        decoded = base64.urlsafe_b64decode(raw["raw"])
        return Email.from_bytes(
            decoded,
            message_id=message_id,
            thread_id=thread_id or raw.get("threadId", ""),
        )

    def iter_messages(
        self, query: str, max_results: int = 100
    ) -> Iterator[Email]:
        for stub in self.search(query, max_results=max_results):
            yield self.fetch(stub["id"], stub.get("threadId", ""))


def _iso_from_ms(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ""


def with_date_window(query: str, since: date | None, lookback_days: int,
                     first_from: date | None = None) -> str:
    """Add an `after:` clause so repeat scans do not re-read the whole mailbox.

    A few days of overlap are deliberate: messages can arrive slightly out of
    order, and the message-id uniqueness constraint makes re-reading harmless.
    The first scan starts at `first_from` (the date chosen when the account
    was connected) if there is one, else `lookback_days` ago.
    """
    if "after:" in query or "newer_than:" in query:
        return query
    if since:
        start = since - timedelta(days=3)
    elif first_from:
        start = first_from
    else:
        start = date.today() - timedelta(days=lookback_days)
    return f"{query} after:{start.strftime('%Y/%m/%d')}"
