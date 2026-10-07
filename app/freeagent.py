"""FreeAgent API: sign-in, reading, and filing (PLAN.md §7).

Writes are off unless a caller sets `writes_allowed`: only app/filer.py
does, and only when dry run is off (PLAN.md §9). Every request uses API
version 2026-09-01, where explanation attachments go through their own
endpoint; that becomes FreeAgent's default on 1 December 2026.

Sign-in is OAuth 2.0. The redirect comes back to this app's own local
server (`/freeagent/callback`), so no second process or port is needed.
The client id and secret are per install, in `freeagent_credentials.json`
next to Google's `credentials.json`; nothing about any one person's
business lives in code (PLAN.md §12).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

HOSTS = {"sandbox": "https://api.sandbox.freeagent.com", "live": "https://api.freeagent.com"}
USER_AGENT = "Receipt Bridge (personal bookkeeping app)"
API_VERSION = "2026-09-01"
REFRESH_MARGIN = 120          # refresh when the access token has < 2 minutes left
MAX_RETRY_AFTER = 60          # never sleep longer than this on a 429


class FreeAgentError(RuntimeError):
    pass


class NotConnected(FreeAgentError):
    """No usable sign-in: connect (or reconnect) FreeAgent in Settings."""


class WritesOff(FreeAgentError):
    """A write was attempted while writes are switched off (dry run)."""


@dataclass
class Credentials:
    client_id: str
    client_secret: str
    environment: str          # "sandbox" or "live"
    redirect_uri: str

    @classmethod
    def load(cls, path: Path, default_redirect: str) -> "Credentials | None":
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        environment = data.get("environment", "sandbox")
        if environment not in HOSTS:
            raise FreeAgentError(f"environment must be sandbox or live, not {environment!r}")
        if not data.get("client_id") or not data.get("client_secret"):
            raise FreeAgentError(f"{path.name} needs client_id and client_secret")
        return cls(data["client_id"], data["client_secret"], environment,
                   data.get("redirect_uri") or default_redirect)

    @property
    def host(self) -> str:
        return HOSTS[self.environment]


class TokenStore:
    """The access and refresh tokens, owner-only on disk, like Gmail's."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def save(self, token: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(token, indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class FreeAgent:
    def __init__(self, credentials: Credentials, store: TokenStore, *,
                 session: Any = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.credentials = credentials
        self.store = store
        self.session = session or requests.Session()
        self.clock = clock
        self.sleep = sleep
        self.writes_allowed = False

    # ---- sign-in ---------------------------------------------------------

    def authorize_url(self, state: str) -> str:
        query = urlencode({
            "client_id": self.credentials.client_id,
            "response_type": "code",
            "redirect_uri": self.credentials.redirect_uri,
            "state": state,
        })
        return f"{self.credentials.host}/v2/approve_app?{query}"

    def exchange_code(self, code: str) -> None:
        self._token_request({"grant_type": "authorization_code", "code": code,
                             "redirect_uri": self.credentials.redirect_uri})

    @property
    def connected(self) -> bool:
        token = self.store.load()
        return bool(token and token.get("environment") == self.credentials.environment
                    and token.get("refresh_token"))

    def disconnect(self) -> None:
        self.store.clear()

    def _token_request(self, body: dict[str, str]) -> None:
        response = self.session.post(
            f"{self.credentials.host}/v2/token_endpoint",
            data=body,
            auth=(self.credentials.client_id, self.credentials.client_secret),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=30,
        )
        if response.status_code in (400, 401):
            # an expired or revoked refresh token, or a bad code
            self.store.clear()
            raise NotConnected("FreeAgent sign-in has expired: reconnect in Settings")
        if response.status_code != 200:
            raise FreeAgentError(f"FreeAgent token request failed ({response.status_code})")
        data = response.json()
        now = self.clock()
        self.store.save({
            "environment": self.credentials.environment,
            "access_token": data["access_token"],
            # FreeAgent issues a new refresh token on every refresh
            "refresh_token": data.get("refresh_token") or (self.store.load() or {}).get("refresh_token"),
            "expires_at": now + float(data.get("expires_in", 3600)),
            "refresh_expires_at": now + float(data["refresh_token_expires_in"])
            if data.get("refresh_token_expires_in") else None,
        })

    def _access_token(self) -> str:
        token = self.store.load()
        if not token or token.get("environment") != self.credentials.environment:
            raise NotConnected("FreeAgent isn't connected")
        if self.clock() >= float(token.get("expires_at", 0)) - REFRESH_MARGIN:
            self._token_request({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
            token = self.store.load() or {}
        return token["access_token"]

    # ---- reading ---------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> requests.Response:
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None) -> requests.Response:
        """A request with sign-in, one retry after a refresh on 401, and
        backing off on 429 as FreeAgent asks (120 requests a minute)."""
        if method != "GET" and not self.writes_allowed:
            raise WritesOff(f"{method} {path} refused: writing to FreeAgent is switched off")
        url = path if path.startswith("http") else f"{self.credentials.host}/v2/{path.lstrip('/')}"
        refreshed = False
        for _attempt in range(5):
            headers = {"Authorization": f"Bearer {self._access_token()}",
                       "Accept": "application/json", "User-Agent": USER_AGENT,
                       "X-Api-Version": API_VERSION}
            if method == "GET":
                response = self.session.get(url, params=params, timeout=30, headers=headers)
            else:
                headers["Content-Type"] = "application/json"
                response = self.session.request(method, url, json=body, timeout=60, headers=headers)
            if response.status_code == 401 and not refreshed:
                token = self.store.load() or {}
                token["expires_at"] = 0           # force a refresh, then retry once
                self.store.save(token)
                refreshed = True
                continue
            if response.status_code == 429:
                wait = min(float(response.headers.get("Retry-After", 5)), MAX_RETRY_AFTER)
                log.info("FreeAgent rate limit: waiting %.0fs", wait)
                self.sleep(wait)
                continue
            if response.status_code == 401:
                raise NotConnected("FreeAgent refused the sign-in: reconnect in Settings")
            if response.status_code >= 400:
                raise FreeAgentError(f"FreeAgent {method} {path} failed ({response.status_code}): "
                                     f"{_errors(response)}")
            return response
        raise FreeAgentError(f"FreeAgent {path}: gave up after repeated rate limiting")

    def get_all(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Every page of a list endpoint (100 per page, following Link: next)."""
        items: list[dict] = []
        response = self.get(path, {**(params or {}), "per_page": 100})
        while True:
            items.extend(response.json().get(key, []))
            following = response.links.get("next", {}).get("url")
            if not following:
                return items
            response = self.get(following)

    def company(self) -> dict:
        return self.get("company").json()["company"]

    def me(self) -> dict:
        return self.get("users/me").json()["user"]

    def bank_accounts(self) -> list[dict]:
        return self.get_all("bank_accounts", "bank_accounts")

    def projects(self) -> list[dict]:
        """Active projects: what a payment or expense can be re-billed to."""
        return self.get_all("projects", "projects", {"view": "active"})

    def contacts(self) -> list[dict]:
        return self.get_all("contacts", "contacts", {"view": "active"})

    def categories(self) -> list[dict]:
        """Spending categories, flattened, each with its group."""
        data = self.get("categories").json()
        out = []
        for group in ("admin_expenses_categories", "cost_of_sales_categories", "general_categories"):
            for category in data.get(group, []):
                out.append({**category, "group": group})
        return out

    def bank_transactions(self, bank_account: str, *, from_date: str | None = None,
                          to_date: str | None = None, updated_since: str | None = None,
                          view: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"bank_account": bank_account}
        for name, value in (("from_date", from_date), ("to_date", to_date),
                            ("updated_since", updated_since), ("view", view)):
            if value:
                params[name] = value
        return self.get_all("bank_transactions", "bank_transactions", params)


    # ---- filing (writes) ------------------------------------------------------

    def get_url(self, url: str) -> dict:
        return self.get(url).json()

    def create_explanation(self, explanation: dict) -> dict:
        response = self._request("POST", "bank_transaction_explanations",
                                 body={"bank_transaction_explanation": explanation})
        return response.json()["bank_transaction_explanation"]

    def add_explanation_attachments(self, explanation_url: str, attachments: list[dict]) -> list[dict]:
        response = self._request("POST", f"{explanation_url}/attachments", body={"attachments": attachments})
        return response.json().get("attachments", [])

    def remove_explanation_attachments(self, explanation_url: str, attachment_urls: list[str]) -> None:
        """Remove only these attachments, leaving any others in place."""
        self._request("PUT", f"{explanation_url}/attachments",
                      body={"attachments": [{"url": u, "_destroy": "true"} for u in attachment_urls]})

    def update_explanation(self, explanation_url: str, changes: dict) -> dict:
        response = self._request("PUT", explanation_url, body={"bank_transaction_explanation": changes})
        return response.json().get("bank_transaction_explanation", {})

    def create_expense(self, expense: dict) -> dict:
        response = self._request("POST", "expenses", body={"expense": expense})
        return response.json()["expense"]

    def update_expense(self, expense_url: str, changes: dict) -> dict:
        response = self._request("PUT", expense_url, body={"expense": changes})
        return response.json().get("expense", {})

    def delete(self, url: str) -> None:
        self._request("DELETE", url)


def _errors(response: Any) -> str:
    """FreeAgent's own error text, short."""
    try:
        data = response.json()
    except Exception:
        return ""
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, dict):
        errors = errors.get("error", errors)
    if isinstance(errors, list):
        return "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)[:300]
    return str(errors or data)[:300]


# The schemes vat_settings reports, and the choices for correcting it.
VAT_SCHEMES = ("standard", "cash accounting", "flat rate", "not registered")


def vat_settings(company: dict) -> dict[str, Any]:
    """The company's VAT position, as far as the API says (PLAN.md §6.2).
    FreeAgent reports the scheme at registration; a later switch isn't
    exposed, so the Settings screen lets the user correct it."""
    registered = bool(company.get("sales_tax_registration_number")) and company.get(
        "sales_tax_registration_status", "Registered") != "De-registered"
    if company.get("sales_tax_deregistration_effective_date"):
        registered = False
    if not registered:
        scheme = "not registered"
    elif company.get("initially_on_frs"):
        scheme = "flat rate"
    elif (company.get("initial_vat_basis") or "").lower() == "cash":
        scheme = "cash accounting"
    else:
        scheme = "standard"
    return {"registered": registered, "scheme": scheme, "currency": company.get("currency", "GBP"),
            "company": company.get("name", "")}
