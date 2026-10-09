"""API and service tests. No network, no browser, no real Gmail.

These guard the three properties the rewrite exists for:

* The UI path never touches the network — a page that waits on Google is
  what made the old app slow and froze it at launch.
* The local server can't be driven by other web pages (CSRF) or by DNS
  rebinding.
* State changes are validated rather than trusted.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.accounts import Account  # noqa: E402
from app.api import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.service import ReceiptService  # noqa: E402


def _service(tmp: Path) -> ReceiptService:
    config = Config(
        raw={
            "data_dir": str(tmp / "data"),
            "export_dir": str(tmp / "exports"),
            "watchers_dir": str(ROOT / "watchers"),
            # never the real iCloud inbox or FreeAgent app
            "photo_inbox": str(tmp / "inbox"),
            "freeagent": {"credentials_file": str(tmp / "freeagent_credentials.json")},
        }
    )
    return ReceiptService(config)


def _client(service: ReceiptService, host: str = "127.0.0.1") -> tuple[TestClient, str]:
    app = create_app(service)
    return TestClient(app, base_url=f"http://{host}"), app.state.token


def _stage(service: ReceiptService, n: int, **extra) -> list[int]:
    pdf = service.config.pdf_dir
    pdf.mkdir(parents=True, exist_ok=True)
    ids = []
    for i in range(n):
        path = pdf / f"r{i}-{extra.get('watcher_id', 'yesim')}.pdf"
        path.write_bytes(b"%PDF-1.4 test")
        ids.append(
            service.db.insert_receipt(
                {
                    "watcher_id": "yesim",
                    "gmail_message_id": f"msg-{i}-{time.time_ns()}",
                    "vendor": "Yesim",
                    "reference": f"REF{i}{time.time_ns()}",
                    "purchased_on": "2026-09-01",
                    "total": 10.0 + i,
                    "currency": "GBP",
                    "filename": f"2026-09-01 Yesim GBP{10 + i}.00 REF{i}.pdf",
                    "pdf_path": str(path),
                    "pdf_source": "rendered_email",
                    **extra,
                }
            )
        )
    return ids


# ---- security -------------------------------------------------------------


def test_api_refuses_requests_without_the_token() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, _ = _client(_service(Path(tmp)))
        assert client.get("/api/state").status_code == 403
        assert client.post("/api/scan").status_code == 403


def test_a_state_change_cannot_be_authorised_by_query_string() -> None:
    """The ?t= form exists for <iframe> GETs only. A hostile page can put a
    query string in a form action; it cannot set a custom header."""
    with tempfile.TemporaryDirectory() as tmp:
        client, token = _client(_service(Path(tmp)))
        assert client.post(f"/api/scan?t={token}").status_code == 403


def test_foreign_host_is_refused_even_with_the_token() -> None:
    """DNS rebinding: a hostile domain resolving to 127.0.0.1."""
    with tempfile.TemporaryDirectory() as tmp:
        client, token = _client(_service(Path(tmp)), host="evil.example")
        response = client.get("/api/state", headers={"X-Receipt-Bridge": token})
        assert response.status_code == 403


def test_the_page_carries_the_token_and_the_api_accepts_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, token = _client(_service(Path(tmp)))
        page = client.get("/")
        assert page.status_code == 200 and token in page.text
        assert client.get("/api/state", headers={"X-Receipt-Bridge": token}).status_code == 200


def test_reveal_cannot_escape_the_export_folder() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, token = _client(_service(Path(tmp)))
        response = client.post(
            "/api/reveal", json={"name": "../../.."}, headers={"X-Receipt-Bridge": token}
        )
        assert response.status_code == 400


# ---- speed ----------------------------------------------------------------


def test_state_never_calls_gmail() -> None:
    """The bug behind the 16-second launch: rendering waited on Google."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        token_dir = service.config.accounts_dir
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "someone@example.com.json").write_text('{"token":"x","refresh_token":"y"}')

        original = Account.check

        def forbidden(*_a, **_k):
            raise AssertionError("the UI path made a network call")

        Account.check = forbidden
        try:
            client, token = _client(service)
            started = time.monotonic()
            response = client.get("/api/state", headers={"X-Receipt-Bridge": token})
            elapsed = time.monotonic() - started
        finally:
            Account.check = original

        assert response.status_code == 200
        assert response.json()["accounts"][0]["state"] == "unknown"
        assert elapsed < 0.5, f"/api/state took {elapsed:.2f}s"


# ---- behaviour ------------------------------------------------------------


def test_ignore_and_restore_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        ids = _stage(service, 2)
        client, token = _client(service)
        h = {"X-Receipt-Bridge": token}

        client.post("/api/receipts/status", json={"ids": [ids[0]], "status": "ignored"}, headers=h)
        assert [r["id"] for r in client.get("/api/receipts?status=ignored", headers=h).json()] == [ids[0]]

        client.post("/api/receipts/status", json={"ids": [ids[0]], "status": "pending"}, headers=h)
        assert len(client.get("/api/receipts?status=pending", headers=h).json()) == 2


def test_deleting_archived_receipts_removes_files_but_remembers_the_email() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        ids = _stage(service, 3)
        client, token = _client(service)
        h = {"X-Receipt-Bridge": token}
        client.post("/api/receipts/status", json={"ids": ids[:2], "status": "ignored"}, headers=h)
        first = service.db.get_receipt(ids[0])

        # one at a time, and only archived ones: a pending receipt is untouched
        r = client.post("/api/archived/delete", json={"ids": [ids[0], ids[2]]}, headers=h)
        assert r.json() == {"deleted": 1}
        assert not Path(first["pdf_path"]).exists()
        assert [x["id"] for x in client.get("/api/receipts?status=ignored", headers=h).json()] == [ids[1]]
        assert service.db.has_message(first["gmail_message_id"])     # a rescan won't collect it again
        assert Path(service.db.get_receipt(ids[2])["pdf_path"]).exists()

        # Clear archive
        assert client.post("/api/archived/delete", json={"all": True}, headers=h).json() == {"deleted": 1}
        assert client.get("/api/receipts?status=ignored", headers=h).json() == []
        assert len(client.get("/api/receipts?status=pending", headers=h).json()) == 1


def test_archived_receipts_are_deleted_after_the_chosen_days() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        ids = _stage(service, 2)
        client, token = _client(service)
        h = {"X-Receipt-Bridge": token}
        client.post("/api/receipts/status", json={"ids": ids, "status": "ignored"}, headers=h)
        service.db.update_receipt(ids[0], {"archived_at": "2026-01-01T00:00:00+00:00"})

        assert service.archive_delete_days == 30                     # on by default
        client.post("/api/settings", json={"archive_delete_days": 0}, headers=h)
        assert service.purge_archived() == 0                         # 0: kept for ever
        assert client.post("/api/settings", json={"archive_delete_days": -1}, headers=h).status_code == 400
        client.post("/api/settings", json={"archive_delete_days": 30}, headers=h)   # purges straight away
        assert client.get("/api/state", headers=h).json()["archive_delete_days"] == 30
        assert [x["id"] for x in client.get("/api/receipts?status=ignored", headers=h).json()] == [ids[1]]


def test_open_at_login_writes_and_removes_the_launch_agent() -> None:
    from app import login_item
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        client, token = _client(service)
        h = {"X-Receipt-Bridge": token}
        saved = (login_item.LAUNCH_AGENT, login_item.INSTALLED, login_item.subprocess.run, login_item.install_app)
        login_item.LAUNCH_AGENT = Path(tmp) / "agent.plist"          # never the real one
        login_item.INSTALLED = Path(tmp) / "Receipt Bridge.app"
        login_item.subprocess.run = lambda *a, **k: None              # no launchctl
        try:
            assert client.get("/api/state", headers=h).json()["open_at_login"] == {"enabled": False, "available": False}
            # no app installed yet: turning it on installs it first, then opens it at login
            built = []
            login_item.install_app = lambda: built.append(1) or login_item.INSTALLED.mkdir() or ""
            service._enqueue = lambda key, job: job() or True
            client.post("/api/settings", json={"open_at_login": True}, headers=h)
            assert built == [1] and service._outcome.ok
            assert str(login_item.INSTALLED) in login_item.LAUNCH_AGENT.read_text()
            assert client.get("/api/state", headers=h).json()["open_at_login"]["enabled"]
            client.post("/api/settings", json={"open_at_login": False}, headers=h)
            assert not login_item.LAUNCH_AGENT.exists()
        finally:
            (login_item.LAUNCH_AGENT, login_item.INSTALLED, login_item.subprocess.run,
             login_item.install_app) = saved


def test_cannot_mark_a_receipt_exported_without_exporting_it() -> None:
    """Otherwise a receipt could vanish from To file with no file written."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        ids = _stage(service, 1)
        client, token = _client(service)
        response = client.post(
            "/api/receipts/status",
            json={"ids": ids, "status": "exported"},
            headers={"X-Receipt-Bridge": token},
        )
        assert response.status_code == 400


def test_export_writes_files_and_moves_receipts_to_filed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        ids = _stage(service, 3)
        client, token = _client(service)
        h = {"X-Receipt-Bridge": token}

        result = client.post("/api/export", json={"ids": ids[:2]}, headers=h).json()
        assert result["count"] == 2
        assert len(list(Path(result["folder"]).glob("*.pdf"))) == 2

        state = client.get("/api/state", headers=h).json()
        assert state["counts"] == {"pending": 1, "exported": 2, "ignored": 0, "failed": 0,
                                   "needs": 1, "missing": 0, "files": 1}
        filed = client.get("/api/receipts?status=exported", headers=h).json()
        assert {r["export_folder"] for r in filed} == {result["name"]}


def test_pending_total_is_reported_per_currency() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        _stage(service, 2)  # 10.00 + 11.00
        _stage(service, 1, currency="EUR", watcher_id="yesim")
        totals = service.snapshot()["totals"]["pending"]
        assert totals["GBP"] == 21.0 and totals["EUR"] == 10.0


def test_receipt_json_describes_documents_in_plain_terms() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        _stage(service, 1)
        row = service.receipts("pending")[0]
        assert row["document"] == "email"
        assert row["has_pdf"] is True


def test_an_email_receipt_is_only_flagged_when_better_was_possible() -> None:
    """Yesim's email *is* its receipt; Trainline's email is a fallback.

    Flagging both the same way cried wolf on every Yesim receipt.
    """
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        _stage(service, 1, watcher_id="yesim")
        _stage(service, 1, watcher_id="trainline")
        kinds = {r["watcher"]: r["document"] for r in service.receipts("pending")}
        assert kinds == {"yesim": "email", "trainline": "fallback"}, kinds


def test_only_missed_supplier_documents_are_retried_automatically() -> None:
    """Trainline email copies are retried; Yesim's emails are the receipt."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        [yesim] = _stage(service, 1, watcher_id="yesim")
        [trainline] = _stage(service, 1, watcher_id="trainline")
        _age(service, [yesim, trainline], hours=13)

        queued = service._queue_automatic_retries()
        assert queued == 1
        assert service.snapshot()["queued"] == [f"retry:{trainline}"]


def test_a_fresh_fallback_is_not_retried_straight_away() -> None:
    """Retrying seconds after the supplier failed just hits the same failure."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        _stage(service, 1, watcher_id="trainline")
        assert service._queue_automatic_retries() == 0


def test_automatic_retries_give_up_after_three_attempts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        [rid] = _stage(service, 1, watcher_id="trainline")
        for _ in range(3):
            service.db.note_fetch_attempt(rid, "2026-01-01T00:00:00+00:00")
        _age(service, [rid], hours=13)
        assert service._queue_automatic_retries() == 0


def test_database_backups_rotate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        folder = service.config.data_dir / "backups"
        folder.mkdir(parents=True)
        for day in range(1, 11):
            (folder / f"receipts-2026-01-{day:02d}.sqlite3").write_bytes(b"old")
        service._backup_database()
        kept = sorted(p.name for p in folder.glob("*.sqlite3"))
        assert len(kept) == 7, kept
        assert any(name.startswith("receipts-20") and "2026-01" not in name for name in kept), (
            "today's backup should be among those kept"
        )


def _age(service: ReceiptService, ids: list[int], hours: int) -> None:
    """Pretend receipts were staged some hours ago."""
    from datetime import datetime, timedelta, timezone

    then = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with service.db.connect() as conn:
        for rid in ids:
            conn.execute("UPDATE receipts SET created_at = ? WHERE id = ?", (then, rid))


def test_duplicate_jobs_are_not_queued_twice() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))  # worker not started: jobs stay queued
        assert service.scan() is True
        assert service.scan() is False
        assert service.retry(7) is True
        assert service.retry(7) is False
        assert service.retry(8) is True


def test_a_failing_job_does_not_kill_the_worker() -> None:
    """If the worker dies, the app silently stops ever scanning again."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        service._enqueue("boom", lambda: 1 / 0)
        ran = []
        service._enqueue("after", lambda: ran.append(True))
        service.start()
        deadline = time.monotonic() + 5
        while not ran and time.monotonic() < deadline:
            time.sleep(0.05)
        service.stop()
        assert ran, "the job after a crash never ran"


def test_a_scan_with_no_working_account_explains_itself() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        service._run_scan("manual")
        outcome = service.snapshot()["outcome"]
        assert outcome["ok"] is False and "Gmail" in outcome["message"]



def test_file_all_always_reports_a_result_even_when_signed_out() -> None:
    """The File all dialog waits for state.file_results; a failure before
    any filing must still end it, or it says "Filing…" forever."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        ids = _stage(service, 1)
        assert service.snapshot()["file_results"] is None
        service._run_file(ids)
        results = service.snapshot()["file_results"]
        assert results is not None and results["results"] == []
        assert "connected" in results["message"]
        assert "file_results" not in service.snapshot()["freeagent"]


def test_the_sidebar_counts_come_from_the_engine() -> None:
    """Match's number is "needs you", the same on every screen (not the
    pending total, and not narrowed by the search box)."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        _stage(service, 2)
        counts = service.snapshot()["counts"]
        assert counts["pending"] == 2
        # no FreeAgent: no category, so both need you; no statement
        assert counts["needs"] == 2 and counts["missing"] == 0



def test_check_now_only_queues_what_is_set_up() -> None:
    """No Gmail and no FreeAgent: "Check now" looks at the photo inbox only,
    and never tries email (an optional tool)."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        client, token = _client(service)
        queued = client.post("/api/check-now", headers={"X-Receipt-Bridge": token}).json()["queued"]
        assert "email" not in queued and "freeagent" not in queued



def test_a_dropped_file_goes_into_the_inbox_ready_to_read() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        client, token = _client(service)
        h = {"X-Receipt-Bridge": token}
        r = client.post("/api/files/upload?name=Lunch%20receipt.jpg&paid_by=personal", content=b"\xff\xd8jpeg", headers=h)
        assert r.status_code == 200, r.text
        from app.photo_inbox import pending_files, _settled
        files = pending_files(service.photo_inbox)
        assert [(p.name.endswith("Lunch receipt.jpg"), who) for p, who in files] == [(True, "personal")]
        assert _settled(files[0][0], time.time()), "a dropped file shouldn't wait to settle"
        assert client.post("/api/files/upload?name=notes.txt", content=b"x", headers=h).status_code == 400
        client.post("/api/files/upload?name=../../evil.jpg", content=b"x", headers=h)   # a path is just a name
        assert all(service.photo_inbox in p.parents for p in Path(tmp).rglob("*evil*"))
        assert client.post("/api/files/upload?name=a.jpg", content=b"x").status_code == 403   # no token



def test_an_email_missing_a_field_is_staged_like_a_photo() -> None:
    """A supplier rule that can't find the total no longer sets the email
    aside as "couldn't read": the general rules read it, with the currency
    printed beside it, and it waits in Files with the total to check."""
    from datetime import datetime
    from app.email_message import Email
    from app.pipeline import ScanReport, process_email
    from app.watchers import load_watchers_safe
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        watchers, _ = load_watchers_safe(ROOT / "watchers")
        uber = next(w for w in watchers if w.id == "uber")
        message = Email(message_id="m-uber-1", thread_id="t1", subject="Your Thursday morning trip with Uber",
                        sender="Uber Receipts <noreply@uber.com>", date=datetime(2026, 9, 28, 9, 0),
                        plain="Thanks for riding\nTotal\nUS$71.08\nTrip fare US$56.00\nUber Technologies Inc. VAT ID 123")
        if not uber.matches(message):
            return                                   # the rule's sender/subject test changed: nothing to check here
        rid = process_email(service.config, service.db, uber, message, None, ScanReport())
        row = service.db.get_receipt(rid)
        assert row["status"] == "pending" and row["total"] == 71.08 and row["currency"] == "USD", dict(row)
        assert any("Total not confirmed" in f for f in json.loads(row["extra_json"])["flags"])



def test_a_supplier_rule_says_who_pays() -> None:
    """Paid with on the rule: its emails arrive as expenses (or tied to one
    bank account), and changing it moves the waiting ones, except any you
    decided yourself."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        a, b = _stage(service, 2)
        service.set_receipt_fields(b, {"paid_by": "business"})            # you chose this one
        assert service.apply_paid_with("yesim", "personal") == 1
        assert service.db.get_receipt(a)["paid_by"] == "personal"
        assert service.db.get_receipt(b)["paid_by"] == "business"
        service.apply_paid_with("yesim", "https://fa.test/v2/bank_accounts/2")
        assert service.db.get_receipt(a)["paid_by"] == "business"
        assert service._bank_account(service.db.get_receipt(a)) == "https://fa.test/v2/bank_accounts/2"

def test_a_supplier_added_again_looks_back_over_its_whole_window() -> None:
    """Scans resume where the last stopped. A rule deleted and added again
    under the same id inherited that, and only saw the last three days."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        queued = []
        service.scan = lambda reason="manual": queued.append(reason) or True
        for key in ("last_scan:me@x.com:example-accountants", "last_scan:example-accountants",
                    "last_scan:me@x.com:accountants", "last_scan:me@x.com:example-accountants-2"):
            service.db.set_state(key, "2026-10-07")
        service.rescan_supplier("example-accountants")
        assert set(service.db.list_state("last_scan:")) == {
            "last_scan:me@x.com:accountants", "last_scan:me@x.com:example-accountants-2"}
        assert queued, "and its receipts are collected now, not in six hours"


def test_the_vat_scheme_can_be_corrected_without_touching_freeagents() -> None:
    """FreeAgent only reports the scheme registered with (vat_settings), so
    Settings can override it; clearing the choice goes back to FreeAgent's."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        client, token = _client(service)
        headers = {"X-Receipt-Bridge": token}
        service.db.set_state("freeagent:reference", json.dumps(
            {"vat": {"registered": True, "scheme": "standard", "company": "Co", "currency": "GBP"}}))

        assert client.post("/api/freeagent/vat-scheme", json={"scheme": "weekly"}, headers=headers).status_code == 400
        assert client.post("/api/freeagent/vat-scheme", json={"scheme": "not registered"}, headers=headers).status_code == 200
        vat = service.freeagent_snapshot()["vat"]
        assert vat == {"registered": False, "scheme": "not registered", "company": "Co", "currency": "GBP",
                       "freeagent_scheme": "standard", "chosen": True}
        # what was read from FreeAgent is kept as it was
        assert json.loads(service.db.get_state("freeagent:reference"))["vat"]["scheme"] == "standard"

        client.post("/api/freeagent/vat-scheme", json={"scheme": ""}, headers=headers)
        vat = service.freeagent_snapshot()["vat"]
        assert vat["scheme"] == "standard" and vat["registered"] and not vat.get("chosen")



def test_a_tidied_photo_can_be_switched_back_to_the_plain_one() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        pdf = service.config.pdf_dir
        pdf.mkdir(parents=True, exist_ok=True)
        plain, tidied, original = pdf / "photo-a.jpg", pdf / "photo-a.crop.jpg", Path(tmp) / "IMG_1.jpg"
        for path, data in ((plain, b"plain"), (tidied, b"tidied"), (original, b"original")):
            path.write_bytes(data)
        tidy = {"used": True, "kind": "crop", "steps": ["cropped to the receipt"], "dropped": [], "notes": [],
                "plain_path": str(plain), "plain_layout": [{"page": 0, "pieces": [{"t": "plain", "b": [0, 0, 1, 1]}]}],
                "tidied_path": str(tidied), "tidied_layout": [{"page": 0, "pieces": [{"t": "tidied", "b": [0, 0, 1, 1]}]}]}
        rid = service.db.insert_receipt({
            "watcher_id": "photo", "source": "photo", "source_id": "sha256:a", "vendor": "Greggs",
            "original_path": str(original), "pdf_path": str(tidied), "pdf_source": "photo", "total": 3.5,
            "extra_json": {"tidy": tidy, "layout": tidy["tidied_layout"]}})
        client, token = _client(service)
        h = {"X-Receipt-Bridge": token}
        item = next(r for r in client.get("/api/receipts", headers=h).json() if r["id"] == rid)
        assert item["tidy"]["on"] and item["tidy"]["steps"] == ["cropped to the receipt"]

        assert client.post(f"/api/receipts/{rid}/tidy", json={"on": False}, headers=h).status_code == 200
        assert client.get(f"/api/receipts/{rid}/pdf", headers=h).content == b"plain"
        layout = json.loads(service.db.get_receipt(rid)["extra_json"])["layout"]
        assert layout[0]["pieces"][0]["t"] == "plain", "highlights must follow the picture"
        assert client.post(f"/api/receipts/{rid}/tidy", json={"on": True}, headers=h).status_code == 200
        assert client.get(f"/api/receipts/{rid}/pdf", headers=h).content == b"tidied"
        assert client.get(f"/api/receipts/{rid}/original", headers=h).content == b"original"


def test_photos_waiting_from_before_tidying_get_tidied_but_keep_their_details() -> None:
    import shutil
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        helper = service.config.data_dir / "bin" / "receipt-reader"     # reuse the built helper
        helper.parent.mkdir(parents=True, exist_ok=True)
        from app.receipt_reader import ensure_built
        shutil.copy2(ensure_built(ROOT / "data"), helper)
        pdf = service.config.pdf_dir
        pdf.mkdir(parents=True, exist_ok=True)
        plain = pdf / "photo-greggs.jpg"
        plain.write_bytes(b"old")
        rid = service.db.insert_receipt({
            "watcher_id": "photo", "source": "photo", "source_id": "sha256:g", "status": "pending",
            "vendor": "Greggs (corrected by hand)", "total": 99.99,
            "original_path": str(ROOT / "tests" / "fixtures" / "photos" / "greggs.webp"),
            "pdf_path": str(plain), "pdf_source": "photo", "extra_json": {"flags": []}})
        service._run_backfill_tidy()
        row = service.db.get_receipt(rid)
        tidy = json.loads(row["extra_json"])["tidy"]
        assert tidy["used"] and row["pdf_path"].endswith(".crop.jpg"), tidy["notes"]
        assert row["vendor"] == "Greggs (corrected by hand)" and row["total"] == 99.99
        assert service._tidy_candidates() == [], "tried again"

def test_about_shows_the_version_the_eula_and_real_licences() -> None:
    from app.updates import VERSION

    with tempfile.TemporaryDirectory() as tmp:
        client, token = _client(_service(Path(tmp)))
        about = client.get("/api/about", headers={"x-receipt-bridge": token}).json()
        assert about["version"] == VERSION and about["copyright"].startswith("©")
        assert "End User Licence Agreement" in about["eula"] and "## 4." in about["eula"]
        names = {p["name"].lower(): p for p in about["open_source"]}
        assert names["fastapi"]["licence"] == "MIT" and names["fastapi"]["has_text"]
        assert "pip" not in names, "the installer's own tools aren't shipped"
        text = client.get("/api/about/licence?name=fastapi", headers={"x-receipt-bridge": token}).json()["text"]
        assert "MIT" in text and "Permission is hereby granted" in text
        assert client.get("/api/about/licence?name=nothing-here", headers={"x-receipt-bridge": token}).status_code == 404


def test_reset_app_goes_back_to_a_fresh_install_but_keeps_original_photos() -> None:
    from app.supplier_builder import HEADER

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        rules = base / "watchers"
        rules.mkdir()
        (rules / "trainline.yaml").write_text("# Trainline booking confirmations.\nid: trainline\n")
        (rules / "uber.yaml").write_text(HEADER + "id: uber\n")
        service = ReceiptService(Config(raw={
            "data_dir": str(base / "data"), "export_dir": str(base / "exports"), "watchers_dir": str(rules),
            "photo_inbox": str(base / "inbox"), "gmail": {"credentials_file": str(base / "credentials.json")},
            "freeagent": {"credentials_file": str(base / "freeagent_credentials.json")}}))
        data = service.config.data_dir
        _stage(service, 2)
        service.db.set_state("pref:theme", "dark")
        service.set_setup_done(True)
        (base / "credentials.json").write_text("{}")
        (base / "freeagent_credentials.json").write_text("{}")
        (data / "accounts").mkdir(exist_ok=True)
        (data / "accounts" / "me@example.test.json").write_text("{}")
        for keep in ("photos/2026/receipt.jpg", "bin/receipt-reader", "app.log"):
            (data / keep).parent.mkdir(parents=True, exist_ok=True)
            (data / keep).write_text("x")
        client, token = _client(service)
        assert client.post("/api/reset", json={}, headers={"x-receipt-bridge": token}).status_code == 400
        assert client.post("/api/reset", json={"confirm": "reset"}, headers={"x-receipt-bridge": token}).json()["queued"]
        service._run_reset()                               # the worker isn't running in tests
        assert service.receipts("pending") == [] and service.db.get_state("pref:theme") is None
        assert not service.snapshot()["setup"]["done"] and service.accounts.list() == []
        assert not (base / "credentials.json").exists() and not (base / "freeagent_credentials.json").exists()
        assert not (data / "pdfs").exists() and not (data / "accounts").exists()
        assert sorted(p.name for p in rules.iterdir()) == ["trainline.yaml"], "only rules made in the app go"
        for keep in ("photos/2026/receipt.jpg", "bin/receipt-reader", "app.log"):
            assert (data / keep).exists(), keep
        assert service.snapshot()["outcome"]["message"] == "Receipt Bridge is reset."


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
