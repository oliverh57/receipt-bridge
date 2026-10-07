"""Notification preferences: which messages are sent, and how often."""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_api import _client, _service  # noqa: E402


class Recorder:
    status = "allowed"

    def __init__(self):
        self.sent: list[str] = []

    def refresh(self, max_age=10.0):
        pass

    def send(self, title, body):
        self.sent.append(body)


def _make():
    tmp = Path(tempfile.mkdtemp())
    service = _service(tmp)
    rec = Recorder()
    service.notifier, service.notify = rec, rec.send
    return service, rec


def test_defaults_send_everything_instantly():
    service, rec = _make()
    prefs = service.notification_prefs()
    assert prefs["frequency"] == "instant" and all(prefs["categories"].values())
    service.notify_about("new", "Found 2 new receipts. Ready to review.")
    assert rec.sent == ["Found 2 new receipts. Ready to review."]


def test_switched_off_category_is_silent_others_are_not():
    service, rec = _make()
    service.set_notification_prefs(categories={"filed": False})
    service.notify_about("filed", "x")
    service.notify_about("new", "y")
    assert rec.sent == ["y"]


def test_daily_holds_messages_until_the_summary_is_due():
    service, rec = _make()
    service.set_notification_prefs(frequency="daily")
    service.notify_about("new", "Found 1 new receipt. Ready to review.")
    service.notify_about("filed", "Got Uber's own receipt for one that only had an email copy.")
    assert rec.sent == [] and service.notification_prefs()["waiting"] == 2
    # already sent a summary today, after 9am: nothing is due
    service.db.set_state("notify:last_summary", datetime.now(timezone.utc).isoformat())
    service.flush_notifications()
    if datetime.now().hour >= service.NOTIFY_DAILY_HOUR:
        assert rec.sent == []
    # last summary was two days ago: due once it is past 9am
    service.db.set_state("notify:last_summary", (datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
    service.flush_notifications()
    if datetime.now().hour >= service.NOTIFY_DAILY_HOUR:
        assert len(rec.sent) == 1 and "2 updates" in rec.sent[0]
        assert service.notification_prefs()["waiting"] == 0


def test_hourly_summary():
    service, rec = _make()
    service.set_notification_prefs(frequency="hourly")
    service.db.set_state("notify:last_summary", datetime.now(timezone.utc).isoformat())
    service.notify_about("new", "Found 1 new receipt. Ready to review.")
    service.flush_notifications()
    assert rec.sent == []
    service.db.set_state("notify:last_summary", (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    service.flush_notifications()
    assert rec.sent == ["Found 1 new receipt."]


def test_master_switch_silences_everything_including_problems():
    service, rec = _make()
    service.set_notification_prefs(frequency="daily")
    service.notify_about("new", "held")
    service.set_notification_prefs(enabled=False)
    assert service.notification_prefs()["waiting"] == 0
    for c in ("new", "filed", "problems"):
        service.notify_about(c, "x")
    assert rec.sent == []
    service.set_notification_prefs(enabled=True, frequency="instant")
    service.notify_about("problems", "back")
    assert rec.sent == ["back"]


def test_problems_are_never_held():
    service, rec = _make()
    service.set_notification_prefs(frequency="daily")
    service.notify_about("problems", "Gmail access has lapsed.")
    assert rec.sent == ["Gmail access has lapsed."]


def test_back_to_instant_releases_held_messages_and_dropping_a_type_discards_its_held_ones():
    service, rec = _make()
    service.set_notification_prefs(frequency="daily")
    service.notify_about("new", "a")
    service.notify_about("filed", "b")
    service.set_notification_prefs(categories={"filed": False})
    assert service.notification_prefs()["waiting"] == 1
    service.set_notification_prefs(frequency="instant")
    assert rec.sent == ["a."] and service.notification_prefs()["waiting"] == 0


def test_api_validates_and_saves():
    service, _ = _make()
    client, token = _client(service)
    url, h = "/api/settings", {"x-receipt-bridge": token}
    r = client.post(url, json={"notify_frequency": "weekly"}, headers=h)
    assert r.status_code == 400, r.text
    r = client.post(url, json={"notify_categories": {"nonsense": True}}, headers=h)
    assert r.status_code == 400, r.text
    r = client.post(url, json={"notify_frequency": "hourly",
                                           "notify_categories": {"new": False}}, headers=h)
    assert r.status_code == 200, r.text
    prefs = service.snapshot()["notification_prefs"]
    assert prefs["frequency"] == "hourly" and prefs["categories"]["new"] is False


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
