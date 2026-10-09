"""Editing supplier rules in place.

The hand-written rules carry comments explaining every decision in them.
The editor's first duty is not to destroy those.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import supplier_editor as ed  # noqa: E402
from app.config import Config  # noqa: E402
from app.service import ReceiptService  # noqa: E402
from app.watchers import Watcher, load_watchers_safe  # noqa: E402


def _copy(name: str, folder: Path) -> Path:
    target = folder / name
    shutil.copy(ROOT / "watchers" / name, target)
    return target


def test_editing_keeps_every_comment_in_a_hand_written_rule() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = _copy("trainline.yaml", Path(tmp))
        comments_before = [l.strip() for l in path.read_text().splitlines() if l.strip().startswith("#")]
        ed.save(path, {"name": "Trainline (work)"})
        comments_after = [l.strip() for l in path.read_text().splitlines() if l.strip().startswith("#")]
        assert comments_before == comments_after, "comments were lost"
        assert Watcher.from_file(path).name == "Trainline (work)"


def test_an_edit_changes_only_the_edited_lines() -> None:
    """No reformatting churn in hand-written files: a rename is two lines."""
    import difflib

    for name in ("trainline.yaml", "yesim.yaml", "freeagent.yaml"):
        with tempfile.TemporaryDirectory() as tmp:
            path = _copy(name, Path(tmp))
            before = path.read_text().splitlines()
            ed.save(path, {"name": "Renamed"})
            changed = [l for l in difflib.ndiff(before, path.read_text().splitlines()) if l[:2] in ("+ ", "- ")]
            assert len(changed) == 4, f"{name}: {changed}"  # name and vendor, old and new



def test_paid_with_is_a_rule_setting() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = _copy("trainline.yaml", Path(tmp))
        assert ed.read(path)["paid_with"] == "business"
        ed.save(path, {"paid_with": "personal"})
        assert Watcher.from_file(path).paid_with == "personal" and "paid_with: personal" in path.read_text()
        ed.save(path, {"paid_with": "https://fa.test/v2/bank_accounts/2"})
        assert Watcher.from_file(path).paid_with == "https://fa.test/v2/bank_accounts/2"
        ed.save(path, {"paid_with": "business"})
        assert "paid_with" not in path.read_text(), "the default leaves the file as it was"
        try:
            ed.save(path, {"paid_with": "the blue card"})
        except ValueError:
            pass
        else:
            raise AssertionError("accepted a made-up account")


def test_renaming_does_not_rewrite_a_tuned_gmail_query() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = _copy("freeagent.yaml", Path(tmp))
        before = Watcher.from_file(path).gmail_query
        ed.save(path, {"name": "FreeAgent subscription"})
        assert Watcher.from_file(path).gmail_query == before


def test_changing_the_match_rebuilds_the_query_to_agree() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = _copy("yesim.yaml", Path(tmp))
        ed.save(path, {"subject": "Payment receipt", "mentions": "Yesim"})
        w = Watcher.from_file(path)
        assert w.match["subject_contains"] == "Payment receipt"
        assert w.gmail_query == 'from:(ecommpay.com) subject:("Payment receipt") "Yesim"'


def test_clearing_an_optional_filter_removes_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = _copy("yesim.yaml", Path(tmp))
        ed.save(path, {"mentions": ""})
        assert "body_contains" not in Watcher.from_file(path).match


def test_a_switched_off_supplier_is_still_listed() -> None:
    """Otherwise switching one off made it vanish, with no way back."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "watchers"
        folder.mkdir()
        path = _copy("yesim.yaml", folder)
        ed.save(path, {"enabled": False})

        enabled, _ = load_watchers_safe(folder)
        assert [w.id for w in enabled] == [], "a disabled rule would still be scanned"

        service = ReceiptService(Config(raw={"data_dir": str(Path(tmp) / "data"), "watchers_dir": str(folder)}))
        listed = {s["id"]: s for s in service.suppliers()}
        assert listed["yesim"]["enabled"] is False


def test_templates_are_not_listed_as_suppliers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "watchers"
        folder.mkdir()
        _copy("yesim.yaml", folder)
        _copy("_example.yaml", folder)
        service = ReceiptService(Config(raw={"data_dir": str(Path(tmp) / "data"), "watchers_dir": str(folder)}))
        assert [s["id"] for s in service.suppliers()] == ["yesim"]


def test_any_supplier_can_be_deleted_and_restored_intact() -> None:
    """Built-ins included — and a deleted rule comes back byte for byte."""
    with tempfile.TemporaryDirectory() as tmp:
        folder, bin_dir = Path(tmp) / "watchers", Path(tmp) / "bin"
        folder.mkdir()
        path = _copy("trainline.yaml", folder)
        original = path.read_text()

        stored = ed.delete(path, bin_dir)
        assert not path.exists()
        assert load_watchers_safe(folder)[0] == [], "a deleted rule would still be scanned"

        restored = ed.restore(stored, bin_dir, folder)
        assert restored.name == "trainline.yaml"
        assert restored.read_text() == original
        assert not (bin_dir / stored).exists()


def test_restoring_never_overwrites_a_supplier_added_since() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder, bin_dir = Path(tmp) / "watchers", Path(tmp) / "bin"
        folder.mkdir()
        stored = ed.delete(_copy("yesim.yaml", folder), bin_dir)
        replacement = _copy("yesim.yaml", folder)
        replacement.write_text(replacement.read_text().replace("name: Yesim", "name: Yesim (new)"))

        restored = ed.restore(stored, bin_dir, folder)
        assert restored.name == "yesim-2.yaml"
        assert "Yesim (new)" in replacement.read_text(), "the newer rule was overwritten"


def test_restore_only_reads_from_the_bin() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder, bin_dir = Path(tmp) / "watchers", Path(tmp) / "bin"
        folder.mkdir()
        bin_dir.mkdir()
        (Path(tmp) / "secret.yaml").write_text("id: x\ngmail_query: q\n")
        for name in ("../secret.yaml", "", ".hidden.yaml", "missing.yaml"):
            try:
                ed.restore(name, bin_dir, folder)
            except ValueError:
                continue
            raise AssertionError(f"restored {name!r}")


def test_trainline_is_built_in_and_cannot_be_deleted() -> None:
    from fastapi.testclient import TestClient

    from app.api import create_app

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "watchers"
        folder.mkdir()
        _copy("trainline.yaml", folder)
        service = ReceiptService(Config(raw={"data_dir": str(Path(tmp) / "data"), "watchers_dir": str(folder)}))
        app = create_app(service)
        client = TestClient(app, base_url="http://127.0.0.1")
        h = {"X-Receipt-Bridge": app.state.token}
        assert client.get("/api/suppliers", headers=h).json()[0]["built_in"] is True
        assert client.get("/api/suppliers/trainline", headers=h).json()["built_in"] is True
        refused = client.post("/api/suppliers/trainline/delete", headers=h)
        assert refused.status_code == 400 and "switch it off" in refused.json()["detail"]
        assert (folder / "trainline.yaml").exists()


def test_deleting_through_the_api_keeps_collected_receipts() -> None:
    from fastapi.testclient import TestClient

    from app.api import create_app

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "watchers"
        folder.mkdir()
        _copy("yesim.yaml", folder)
        service = ReceiptService(Config(raw={"data_dir": str(Path(tmp) / "data"), "watchers_dir": str(folder)}))
        service.db.insert_receipt({"watcher_id": "yesim", "gmail_message_id": "m1", "vendor": "Yesim",
                                   "filename": "r.pdf", "total": 8.64, "currency": "GBP"})
        app = create_app(service)
        client = TestClient(app, base_url="http://127.0.0.1")
        h = {"X-Receipt-Bridge": app.state.token}

        gone = client.post("/api/suppliers/yesim/delete", headers=h).json()
        assert gone["name"] == "Yesim" and gone["undo"]
        assert client.get("/api/suppliers", headers=h).json() == []
        assert len(service.receipts("pending")) == 1, "collected receipts were lost"

        back = client.post("/api/suppliers/restore", json={"undo": gone["undo"]}, headers=h).json()
        assert back["name"] == "Yesim"
        assert [s["id"] for s in client.get("/api/suppliers", headers=h).json()] == ["yesim"]


def test_invalid_edits_are_refused_and_nothing_is_written() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = _copy("trainline.yaml", Path(tmp))
        original = path.read_text()
        for bad in ({"name": "  "}, {"domain": ""}):
            try:
                ed.save(path, bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad} was accepted")
        assert path.read_text() == original


def test_temporary_files_are_never_loaded_as_rules() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _copy("yesim.yaml", folder)
        (folder / ".yesim-half-written.yaml").write_text("id: broken\n  nonsense: [")
        watchers, problems = load_watchers_safe(folder)
        assert [w.id for w in watchers] == ["yesim"] and not problems


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
