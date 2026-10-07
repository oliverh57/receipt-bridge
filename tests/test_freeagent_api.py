"""FreeAgent sign-in, token handling, reading, and the callback route.
A fake FreeAgent stands in for the real one: no network. Made-up values
throughout (PLAN.md §12).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.freeagent import Credentials, FreeAgent, NotConnected, TokenStore, vat_settings  # noqa: E402
from app.service import ReceiptService  # noqa: E402

HOST = "https://api.sandbox.freeagent.com"


class Response:
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None,
                 links: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.links = links or {}

    def json(self) -> dict:
        return self._body


class FakeFreeAgent:
    """Records requests; answers from a queue per URL."""

    def __init__(self):
        self.posts: list[dict] = []
        self.gets: list[tuple[str, dict]] = []
        self.queued: dict[str, list[Response]] = {}
        self.token_responses: list[Response] = []

    def post(self, url, data=None, auth=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data, "auth": auth})
        return self.token_responses.pop(0)

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, {**(params or {}), "_auth": (headers or {}).get("Authorization")}))
        return self.queued[url].pop(0)


def token(access: str, refresh: str, expires_in: int = 3600) -> Response:
    return Response(200, {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
                          "expires_in": expires_in, "refresh_token_expires_in": 630000000})


def make_client(tmp: Path, now: list[float] | None = None):
    fake = FakeFreeAgent()
    clock = now or [1000.0]
    slept: list[float] = []
    client = FreeAgent(Credentials("id-123", "secret-456", "sandbox", "http://127.0.0.1:8765/freeagent/callback"),
                       TokenStore(tmp / "token.json"), session=fake,
                       clock=lambda: clock[0], sleep=slept.append)
    return client, fake, clock, slept


def test_authorize_url_carries_state_and_redirect() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, *_ = make_client(Path(tmp))
        url = urlparse(client.authorize_url("abc"))
        query = parse_qs(url.query)
        assert f"{url.scheme}://{url.netloc}{url.path}" == f"{HOST}/v2/approve_app"
        assert query == {"client_id": ["id-123"], "response_type": ["code"], "state": ["abc"],
                         "redirect_uri": ["http://127.0.0.1:8765/freeagent/callback"]}


def test_code_exchange_stores_tokens_owner_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake, *_ = make_client(Path(tmp))
        fake.token_responses.append(token("A1", "R1"))
        client.exchange_code("code-1")
        assert fake.posts[0]["auth"] == ("id-123", "secret-456"), "client credentials by HTTP Basic"
        assert fake.posts[0]["data"]["grant_type"] == "authorization_code"
        assert client.connected
        assert (Path(tmp) / "token.json").stat().st_mode & 0o777 == 0o600


def test_an_expiring_token_is_refreshed_and_the_new_refresh_token_kept() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake, clock, _ = make_client(Path(tmp))
        fake.token_responses += [token("A1", "R1"), token("A2", "R2")]
        client.exchange_code("code-1")
        clock[0] += 3600                      # an hour later: expired
        fake.queued[f"{HOST}/v2/company"] = [Response(200, {"company": {"name": "Example Ltd"}})]
        assert client.company()["name"] == "Example Ltd"
        assert fake.posts[1]["data"] == {"grant_type": "refresh_token", "refresh_token": "R1"}
        assert fake.gets[0][1]["_auth"] == "Bearer A2"
        assert json.loads((Path(tmp) / "token.json").read_text())["refresh_token"] == "R2"


def test_a_401_refreshes_once_and_retries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake, *_ = make_client(Path(tmp))
        fake.token_responses += [token("A1", "R1"), token("A2", "R2")]
        client.exchange_code("code-1")
        fake.queued[f"{HOST}/v2/users/me"] = [Response(401), Response(200, {"user": {"email": "x@example.com"}})]
        assert client.me()["email"] == "x@example.com"
        assert [g[1]["_auth"] for g in fake.gets] == ["Bearer A1", "Bearer A2"]


def test_a_revoked_sign_in_disconnects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake, clock, _ = make_client(Path(tmp))
        fake.token_responses += [token("A1", "R1"), Response(400, {"error": "invalid_grant"})]
        client.exchange_code("code-1")
        clock[0] += 7200
        try:
            client.company()
        except NotConnected:
            pass
        else:
            raise AssertionError("expected NotConnected")
        assert not client.connected


def test_rate_limits_back_off_as_asked() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake, _, slept = make_client(Path(tmp))
        fake.token_responses.append(token("A1", "R1"))
        client.exchange_code("code-1")
        fake.queued[f"{HOST}/v2/company"] = [Response(429, headers={"Retry-After": "7"}),
                                             Response(200, {"company": {}})]
        client.company()
        assert slept == [7.0]


def test_list_endpoints_follow_every_page() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake, *_ = make_client(Path(tmp))
        fake.token_responses.append(token("A1", "R1"))
        client.exchange_code("code-1")
        page2 = f"{HOST}/v2/bank_transactions?page=2"
        fake.queued[f"{HOST}/v2/bank_transactions"] = [
            Response(200, {"bank_transactions": [{"url": "t/1"}, {"url": "t/2"}]}, links={"next": {"url": page2}})]
        fake.queued[page2] = [Response(200, {"bank_transactions": [{"url": "t/3"}]})]
        rows = client.bank_transactions("acct/1", from_date="2026-01-01")
        assert [r["url"] for r in rows] == ["t/1", "t/2", "t/3"]
        assert fake.gets[0][1]["per_page"] == 100 and fake.gets[0][1]["bank_account"] == "acct/1"


def test_vat_scheme_from_company_settings() -> None:
    assert vat_settings({"sales_tax_registration_number": "123"})["scheme"] == "standard"
    assert vat_settings({"sales_tax_registration_number": "123", "initially_on_frs": True})["scheme"] == "flat rate"
    assert vat_settings({"sales_tax_registration_number": "123", "initial_vat_basis": "Cash"})["scheme"] == "cash accounting"
    assert vat_settings({})["scheme"] == "not registered"


# ---- the callback route on the app's own server ------------------------------------

def service_with_credentials(tmp: Path) -> ReceiptService:
    creds = tmp / "freeagent_credentials.json"
    creds.write_text(json.dumps({"client_id": "id-123", "client_secret": "secret-456", "environment": "sandbox"}))
    config = Config(raw={"data_dir": str(tmp / "data"), "export_dir": str(tmp / "exports"),
                         "watchers_dir": str(ROOT / "watchers"), "photo_inbox": str(tmp / "inbox"),
                         "freeagent": {"credentials_file": str(creds)}})
    return ReceiptService(config)


def test_callback_accepts_only_a_state_this_app_issued_and_only_once() -> None:
    import app.service as service_module
    with tempfile.TemporaryDirectory() as tmp:
        service = service_with_credentials(Path(tmp))
        opened: list = []
        real_run = service_module.subprocess.run
        service_module.subprocess.run = lambda args, **kw: opened.append(args)   # don't open a browser
        try:
            app = create_app(service)
            web = TestClient(app, base_url="http://127.0.0.1")
            started = web.post("/api/freeagent/connect", headers={"x-receipt-bridge": app.state.token})
            assert started.status_code == 200
            state = parse_qs(urlparse(started.json()["url"]).query)["state"][0]
            assert opened and opened[0][0] == "open"

            assert web.get("/freeagent/callback", params={"code": "c", "state": "forged"}).status_code == 400
            assert web.get("/freeagent/callback", params={"code": "c", "state": state}).status_code == 200
            assert "freeagent-connect" in service.snapshot()["queued"]
            assert web.get("/freeagent/callback", params={"code": "c", "state": state}).status_code == 400, \
                "a state is single-use"
        finally:
            service_module.subprocess.run = real_run


def test_callback_refuses_foreign_hosts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = service_with_credentials(Path(tmp))
        web = TestClient(create_app(service), base_url="http://evil.example")
        assert web.get("/freeagent/callback", params={"code": "c", "state": "s"}).status_code == 403


def test_the_snapshot_reports_freeagent_without_calling_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        snap = service_with_credentials(Path(tmp)).snapshot()["freeagent"]
        assert snap["has_credentials"] and snap["environment"] == "sandbox" and not snap["connected"]


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
