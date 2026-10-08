"""The licence file: the app keys, handed out and dropped on the app."""

from __future__ import annotations

import json
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import licence  # noqa: E402
from tests.test_api import _client, _service  # noqa: E402

GOOGLE = {"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "g-secret",
                        "redirect_uris": ["http://localhost"]}}
FREEAGENT = {"client_id": "fa-id", "client_secret": "fa-secret", "environment": "live"}


def _licence(**parts) -> bytes:
    return json.dumps({"receipt_bridge_licence": 1, **parts}).encode()


def test_make_then_install_round_trips_both_keys() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "g.json").write_text(json.dumps(GOOGLE))
        (tmp_path / "f.json").write_text(json.dumps(FREEAGENT))
        data = licence.make(tmp_path / "g.json", tmp_path / "f.json")

        target = tmp_path / "copy"
        assert licence.install(data, target / "credentials.json", target / "freeagent.json") == ["Google", "FreeAgent"]
        assert json.loads((target / "credentials.json").read_text()) == GOOGLE
        assert json.loads((target / "freeagent.json").read_text()) == FREEAGENT
        assert stat.S_IMODE((target / "freeagent.json").stat().st_mode) == 0o600     # owner only


def test_anything_else_is_refused_in_words() -> None:
    for data, words in ((b"\x89PNG not json", "isn't a Receipt Bridge licence"),
                        (json.dumps({"installed": GOOGLE["installed"]}).encode(), "isn't a Receipt Bridge licence"),
                        (_licence(), "no keys"),
                        (_licence(freeagent={"client_id": "x"}), "FreeAgent key is incomplete"),
                        (_licence(google={"installed": {}}), "Google key is incomplete")):
        try:
            licence.parse(data)
        except licence.LicenceError as exc:
            assert words in str(exc), (words, exc)
        else:
            raise AssertionError(f"expected {words!r}")


def test_dropping_the_licence_connects_the_app() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        service.config.raw["gmail"] = {"credentials_file": str(Path(tmp) / "credentials.json")}
        client, token = _client(service)
        h = {"x-receipt-bridge": token}
        state = client.get("/api/state", headers=h).json()
        assert state["needs_licence"] and not state["has_credentials"] and not state["freeagent"]["has_credentials"]

        bad = client.post("/api/licence", content=b"hello", headers=h)
        assert bad.status_code == 400 and "isn't a Receipt Bridge licence" in bad.json()["detail"]

        ok = client.post("/api/licence", content=_licence(google=GOOGLE, freeagent=FREEAGENT), headers=h)
        assert ok.json() == {"installed": ["Google", "FreeAgent"]}
        state = client.get("/api/state", headers=h).json()
        assert not state["needs_licence"] and state["has_credentials"] and state["freeagent"]["has_credentials"]

        # a change of state needs the token, like every other
        assert client.post("/api/licence", content=_licence(google=GOOGLE)).status_code == 403


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
