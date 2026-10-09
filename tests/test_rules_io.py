"""Export and import of recurring receipts (supplier rules). No network."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import rules_io  # noqa: E402
from app.api import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.service import ReceiptService  # noqa: E402

EXAMPLE = (ROOT / "watchers" / "_example.yaml").read_text()


def rule(rule_id: str, name: str) -> str:
    """A real, loadable rule: the repo's example, renamed and switched on."""
    return (EXAMPLE.replace("id: example", f"id: {rule_id}").replace("name: Example Supplier", f"name: {name}")
            .replace("enabled: false", "enabled: true"))


def make(rules: dict[str, str]) -> tuple[ReceiptService, Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    folder = base / "watchers"
    folder.mkdir()
    for file, text in rules.items():
        (folder / file).write_text(text)
    service = ReceiptService(Config(raw={"data_dir": str(base / "data"), "export_dir": str(base / "e"),
                                         "watchers_dir": str(folder), "photo_inbox": str(base / "inbox"),
                                         "freeagent": {"credentials_file": str(base / "none.json")}}))
    service.rescan_supplier = mock.Mock()          # no Gmail in tests
    return service, folder, tmp


def test_export_has_only_the_ticked_rules_exactly_as_written() -> None:
    uber = "# my notes about Uber\n" + rule("uber", "Uber")
    service, _, tmp = make({"uber.yaml": uber, "pret.yaml": rule("pret", "Pret")})
    with tmp:
        data, count = service.export_rules(["uber"])
        packed = json.loads(data)
        assert count == 1 and packed["receipt_bridge_rules"] == 1
        assert packed["rules"] == [{"file": "uber.yaml", "yaml": uber}], "comments and all"
        try:
            service.export_rules([])
            raise AssertionError("exported nothing")
        except ValueError as exc:
            assert "Tick at least one" in str(exc)


def test_import_adds_new_rules_and_never_overwrites_one_you_have() -> None:
    mine = "# edited by hand\n" + rule("uber", "Uber")
    service, folder, tmp = make({"uber.yaml": mine})
    with tmp:
        export = rules_io.bundle([("uber.yaml", rule("uber", "Uber (theirs)")), ("pret.yaml", rule("pret", "Pret")),
                                  ("bad.yaml", "id: broken\nnot: a rule")])
        result = service.import_rules(export, "Recurring receipts.rbrules")
        assert [r["name"] for r in result["added"]] == ["Pret"]
        why = {r["name"]: r["why"] for r in result["skipped"]}
        assert why["Uber (theirs)"] == "you already have it" and "isn't a working rule" in why["bad.yaml"]
        assert (folder / "uber.yaml").read_text() == mine, "yours is untouched"
        assert {w.id for w in service.all_suppliers()[0]} == {"uber", "pret"}
        service.rescan_supplier.assert_called_once_with("pret")


def test_a_single_rule_yaml_imports_too_but_other_files_dont() -> None:
    service, folder, tmp = make({})
    with tmp:
        result = service.import_rules(rule("trainline-2", "Trainline").encode(), "Trainline.yaml")
        assert result["added"] == [{"id": "trainline-2", "name": "Trainline"}]
        assert (folder / "trainline.yaml").exists()
        for data, name in [(b"hello", "notes.txt"), (b'{"receipt_bridge_rules": 1, "rules": []}', "x.rbrules")]:
            try:
                service.import_rules(data, name)
                raise AssertionError(f"imported {name}")
            except rules_io.RulesError:
                pass


def test_the_api_imports_a_dropped_file() -> None:
    service, _, tmp = make({})
    with tmp:
        app = create_app(service)
        client, token = TestClient(app, base_url="http://127.0.0.1"), app.state.token
        body = rules_io.bundle([("pret.yaml", rule("pret", "Pret"))])
        res = client.post("/api/suppliers/import?name=Recurring%20receipts.rbrules", content=body,
                          headers={"x-receipt-bridge": token})
        assert res.status_code == 200 and res.json()["added"][0]["name"] == "Pret"
        bad = client.post("/api/suppliers/import?name=x.txt", content=b"nope", headers={"x-receipt-bridge": token})
        assert bad.status_code == 400
        assert client.post("/api/suppliers/export", json={"ids": []},
                           headers={"x-receipt-bridge": token}).status_code == 400


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
