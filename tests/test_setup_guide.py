"""First-run setup guide: where the receipt inbox is made, the iPhone
Shortcut's settings file, and the guide's own state. A throwaway "iCloud
Drive" and "Shortcuts" folder stand in for the real ones, which are never
touched.
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import setup_guide  # noqa: E402
from app.api import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.service import ReceiptService  # noqa: E402


@contextmanager
def fake_icloud():
    """A temporary iCloud Drive and Shortcuts folder in place of the real ones."""
    saved = setup_guide.ICLOUD_DRIVE, setup_guide.SHORTCUTS_FOLDER
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        setup_guide.ICLOUD_DRIVE = base / "CloudDocs"
        setup_guide.SHORTCUTS_FOLDER = base / "Shortcuts"
        setup_guide.ICLOUD_DRIVE.mkdir()
        setup_guide.SHORTCUTS_FOLDER.mkdir()
        try:
            yield base
        finally:
            setup_guide.ICLOUD_DRIVE, setup_guide.SHORTCUTS_FOLDER = saved


def _service(base: Path, **raw) -> ReceiptService:
    return ReceiptService(Config(raw={
        "data_dir": str(base / "data"), "export_dir": str(base / "export"),
        "photo_inbox": str(setup_guide.ICLOUD_DRIVE / "Receipt Inbox"),
        "freeagent": {"credentials_file": str(base / "none.json")}, **raw}))


def _settings_file() -> Path:
    return setup_guide.SHORTCUTS_FOLDER / setup_guide.SHORTCUT_SETTINGS


def test_inbox_for_a_chosen_folder() -> None:
    with fake_icloud() as base:
        assert setup_guide.inbox_for("icloud") == setup_guide.ICLOUD_DRIVE / "Receipt Inbox"
        dropbox = base / "Dropbox"
        dropbox.mkdir()
        assert setup_guide.inbox_for(str(dropbox)) == dropbox / "Receipt Inbox", "a Receipt Inbox is made inside"
        named = dropbox / "Receipt Inbox"
        named.mkdir()
        assert setup_guide.inbox_for(str(named)) == named, "choosing the inbox itself uses it"
        sorted_already = base / "Receipts"
        for sub in ("Bank", "Expense"):
            (sorted_already / sub).mkdir(parents=True)
        assert setup_guide.inbox_for(str(sorted_already)) == sorted_already, "a folder with Bank/Expense is an inbox"
        for bad in (str(base / "missing"), "relative/path"):
            try:
                setup_guide.inbox_for(bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted {bad}")


def test_icloud_relative() -> None:
    with fake_icloud() as base:
        assert setup_guide.icloud_relative(setup_guide.ICLOUD_DRIVE / "Receipt Inbox") == "Receipt Inbox"
        assert setup_guide.icloud_relative(setup_guide.ICLOUD_DRIVE / "Work" / "Receipts") == "Work/Receipts"
        assert setup_guide.icloud_relative(setup_guide.ICLOUD_DRIVE) is None, "iCloud Drive itself isn't a folder in it"
        assert setup_guide.icloud_relative(base / "Dropbox") is None


def test_setup_inbox_makes_folders_and_tells_the_shortcut() -> None:
    with fake_icloud() as base:
        service = _service(base)
        inbox = service.setup_inbox("icloud")
        assert inbox == setup_guide.ICLOUD_DRIVE / "Receipt Inbox"
        assert (inbox / "Bank").is_dir() and (inbox / "Expense").is_dir()
        assert service.db.get_state("pref:photo_inbox") is None, "the default stays the default"
        assert _settings_file().read_text().strip() == "Receipt Inbox"
        snap = service.snapshot()
        assert snap["photo_inbox"]["has_subfolders"]
        assert snap["setup"]["icloud_inbox"] == snap["setup"]["shortcut_saves_to"] == "Receipt Inbox"

        # into a folder in iCloud Drive: the Shortcut follows
        work = setup_guide.ICLOUD_DRIVE / "Work"
        work.mkdir()
        service.setup_inbox(str(work))
        assert _settings_file().read_text().strip() == "Work/Receipt Inbox"

        # out of iCloud Drive: the Shortcut's file goes, so it asks
        dropbox = base / "Dropbox"
        dropbox.mkdir()
        service.setup_inbox(str(dropbox))
        assert service.photo_inbox == dropbox / "Receipt Inbox"
        assert not _settings_file().exists()
        assert service.snapshot()["setup"]["icloud_inbox"] is None


def test_setup_inbox_refuses_the_archive() -> None:
    with fake_icloud() as base:
        service = _service(base)
        try:
            service.setup_inbox(str(service.photo_archive))       # would make an inbox inside it
        except ValueError:
            return
        raise AssertionError("an inbox around the archive would read archived photos as new")


def test_inbox_outside_icloud_leaves_the_file_alone() -> None:
    """Tests and development copies use throwaway inboxes: the real
    Shortcut's file must survive them."""
    with fake_icloud() as base:
        _settings_file().write_text("Receipt Inbox\n")
        elsewhere = base / "tmp-inbox"
        elsewhere.mkdir()
        service = _service(base, photo_inbox=str(elsewhere))
        service.create_inbox_folders()
        other = base / "other"
        other.mkdir()
        service.set_folders(inbox=str(other))
        service.reset_folder("inbox")
        assert _settings_file().read_text().strip() == "Receipt Inbox"


def test_setup_state() -> None:
    with fake_icloud() as base:
        service = _service(base)
        assert service.snapshot()["setup"]["done"] is False, "a new copy shows the guide"
        service.set_setup_done(True)
        assert service.snapshot()["setup"]["done"] is True

        # set up before the guide existed: FreeAgent connected → never shown
        older = _service(base / "older")
        token = older.config.freeagent_token_file
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("{}")
        older._adopt_setup_state()
        assert older.snapshot()["setup"]["done"] is True


def test_demo_copy_uses_stand_in_folders() -> None:
    """config.yaml `setup:` swaps iCloud Drive and Shortcuts for other
    folders, so a demo copy's "Use iCloud Drive" can't reach the real inbox."""
    with fake_icloud() as base:
        cloud, shortcuts = base / "demo-cloud", base / "demo-shortcuts"
        cloud.mkdir()
        shortcuts.mkdir()
        service = _service(base, setup={"icloud_drive": str(cloud), "shortcuts_folder": str(shortcuts)},
                           photo_inbox=str(cloud / "Receipt Inbox"))
        inbox = service.setup_inbox("icloud")
        assert inbox == (cloud / "Receipt Inbox").resolve(), inbox
        assert (shortcuts / "Receipt Bridge.txt").read_text().strip() == "Receipt Inbox"


def test_api() -> None:
    with fake_icloud() as base:
        service = _service(base, iphone={"shortcut_url": "https://www.icloud.com/shortcuts/abc123"})
        app = create_app(service)
        client = TestClient(app, base_url="http://127.0.0.1")
        headers = {"x-receipt-bridge": app.state.token}

        assert client.post("/api/setup/inbox", json={"location": "icloud"}).status_code == 403, "needs the token"
        r = client.post("/api/setup/inbox", json={"location": "icloud"}, headers=headers)
        assert r.status_code == 200 and r.json()["path"].endswith("Receipt Inbox"), r.text
        assert client.post("/api/setup/inbox", json={"location": str(base / "nope")}, headers=headers).status_code == 400
        assert client.post("/api/setup/inbox", json={}, headers=headers).status_code == 400

        assert client.post("/api/setup/done", json={"done": True}, headers=headers).status_code == 200
        assert service.snapshot()["setup"]["done"]

        qr = client.get(f"/api/setup/shortcut-qr?t={app.state.token}")
        assert qr.status_code == 200 and qr.content[:8] == b"\x89PNG\r\n\x1a\n", qr.status_code
        assert client.get("/api/setup/shortcut-qr").status_code == 403

        bare = _service(base / "bare", iphone={"shortcut_url": ""})
        setup_guide_url, setup_guide.SHORTCUT_URL = setup_guide.SHORTCUT_URL, ""
        bare_app = create_app(bare)
        assert TestClient(bare_app, base_url="http://127.0.0.1").get(
            f"/api/setup/shortcut-qr?t={bare_app.state.token}").status_code == 404, "no link, no code"
        setup_guide.SHORTCUT_URL = setup_guide_url
        assert _service(base / "shipped").config.shortcut_url.startswith("https://www.icloud.com/shortcuts/"), \
            "every copy offers the shipped link"


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
