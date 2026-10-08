"""Updates: the version check, GitHub's answers, and installing one. No
network: GitHub is faked, and installs go into a throwaway project folder."""

from __future__ import annotations

import base64
import io
import json
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from app import updates  # noqa: E402
from tests.test_api import _client, _service  # noqa: E402

REPO = "o/r"
BASE = f"{updates.API}/repos/{REPO}"


class FakeResponse:
    def __init__(self, status: int, body=None, raw: bytes = b""):
        self.status_code = status
        self._body = body
        self._raw = raw

    def json(self):
        return self._body

    def iter_content(self, size):
        for i in range(0, len(self._raw), size):
            yield self._raw[i:i + size]


class FakeGitHub:
    """Answers by full URL; anything else is a 404."""

    def __init__(self, answers: dict[str, FakeResponse]):
        self.answers = answers

    def get(self, url, headers=None, timeout=None, **kwargs):
        return self.answers.get(url, FakeResponse(404))


def _source(version: str) -> str:
    return f'"""Receipt Bridge."""\nVERSION = "{version}"\n'


def _tarball(files: dict[str, str]) -> bytes:
    """A GitHub-style source tarball: everything inside owner-repo-sha/."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"o-r-abc123/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _project(tmp: Path) -> Path:
    project = tmp / "project"
    for name, text in {
        "app/updates.py": _source("1.0.0"),
        "app/service.py": "old service",
        "app/unchanged.py": "same",
        "config.yaml": "mine",
        "watchers/trainline.yaml": "edited in Settings",
        "data/receipts.sqlite3": "receipts",
        "requirements.txt": "fastapi\n",
    }.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text)
    return project


NEW_FILES = {
    "app/updates.py": _source("1.1.0"),
    "app/service.py": "new service",
    "app/unchanged.py": "same",
    "app/brand_new.py": "added",
    "config.yaml": "theirs",
    "watchers/trainline.yaml": "upstream",
    "watchers/uber.yaml": "a new supplier",
    "data/receipts.sqlite3": "should never land",
    "requirements.txt": "fastapi\n",
}


def test_versions_compare_as_numbers_not_text() -> None:
    assert updates.parse("v1.10.0") == (1, 10, 0)
    assert updates.parse("2") == (2, 0, 0)
    assert updates.parse("1.2.0-beta") == (1, 2, 0)
    assert updates.parse("latest") is None
    assert updates.is_newer("v1.10.0", "1.9.0")
    assert updates.is_newer("1.0.1", "1.0")
    assert not updates.is_newer("v1.0.0", "1.0.0")
    assert not updates.is_newer("0.9", "1.0.0")
    assert not updates.is_newer("nightly", "1.0.0")


def _branch(version: str) -> dict[str, FakeResponse]:
    return {
        BASE: FakeResponse(200, {"default_branch": "main"}),
        f"{BASE}/commits/main": FakeResponse(200, {"sha": "abc123", "commit": {
            "message": "Faster matching", "committer": {"date": "2026-10-07T10:00:00Z"}}}),
        f"{BASE}/contents/app/updates.py?ref=abc123": FakeResponse(200, {
            "content": base64.b64encode(_source(version).encode()).decode()}),
    }


def _release(tag: str) -> dict[str, FakeResponse]:
    return {f"{BASE}/releases/latest": FakeResponse(200, {
        "tag_name": tag, "html_url": f"https://github.com/o/r/releases/tag/{tag}",
        "body": "  Longer notes.\n", "published_at": "2026-10-01T09:00:00Z",
        "tarball_url": f"{BASE}/tarball/{tag}"})}


def test_pushing_a_new_version_is_enough() -> None:
    found = updates.latest(REPO, session=FakeGitHub(_branch("1.3.0")))
    assert found["version"] == "1.3.0"
    assert found["download"] == f"{BASE}/tarball/abc123"      # that exact commit, not whatever's newest later
    assert found["notes"] == "Faster matching"


def test_a_release_of_the_same_version_supplies_its_notes() -> None:
    found = updates.latest(REPO, session=FakeGitHub({**_branch("1.3.0"), **_release("v1.3.0")}))
    assert found["version"] == "v1.3.0" and found["notes"] == "Longer notes."
    assert found["download"] == f"{BASE}/tarball/v1.3.0"


def test_an_old_release_does_not_hide_a_newer_push() -> None:
    found = updates.latest(REPO, session=FakeGitHub({**_branch("1.3.0"), **_release("v1.2.0")}))
    assert found["version"] == "1.3.0"


def test_problems_are_explained_in_words() -> None:
    cases = [
        (FakeGitHub({}), "Couldn't find o/r"),
        (FakeGitHub({BASE: FakeResponse(403)}), "limiting requests"),
    ]
    for github, words in cases:
        try:
            updates.latest(REPO, session=github)
        except updates.UpdateError as exc:
            assert words in str(exc), exc
        else:
            raise AssertionError(f"expected an error mentioning {words!r}")

    offline = mock.Mock()
    offline.get.side_effect = requests.ConnectionError("down")
    try:
        updates.latest(REPO, session=offline)
    except updates.UpdateError as exc:
        assert "internet" in str(exc)
    else:
        raise AssertionError("expected an error when GitHub can't be reached")


def test_install_replaces_the_code_and_keeps_this_macs_own_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(Path(tmp))
        github = FakeGitHub({f"{BASE}/tarball/abc123": FakeResponse(200, raw=_tarball(NEW_FILES))})
        result = updates.install(f"{BASE}/tarball/abc123", "1.1.0", REPO, project=project,
                                 session=github, log=lambda _line: None)

        assert result == {"version": "1.1.0", "files": 4, "requirements": False, "app": False}
        assert (project / "app/service.py").read_text() == "new service"
        assert (project / "app/brand_new.py").read_text() == "added"
        assert (project / "watchers/uber.yaml").read_text() == "a new supplier"
        # this Mac's own: never replaced
        assert (project / "config.yaml").read_text() == "mine"
        assert (project / "watchers/trainline.yaml").read_text() == "edited in Settings"
        assert (project / "data/receipts.sqlite3").read_text() == "receipts"
        # what was replaced is kept
        with tarfile.open(project / "data/updates/before-1.1.0.tar.gz") as tar:
            assert sorted(tar.getnames()) == ["app/service.py", "app/updates.py"]


def test_install_puts_everything_back_if_it_fails_part_way() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(Path(tmp))
        github = FakeGitHub({f"{BASE}/tarball/abc123": FakeResponse(200, raw=_tarball(NEW_FILES))})
        real_copy = updates.shutil.copy2
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 3:
                raise OSError("disk full")
            return real_copy(src, dst)

        with mock.patch.object(updates.shutil, "copy2", flaky):
            try:
                updates.install(f"{BASE}/tarball/abc123", "1.1.0", REPO, project=project,
                                session=github, log=lambda _line: None)
            except updates.UpdateError as exc:
                assert "nothing changed" in str(exc)
            else:
                raise AssertionError("expected the install to fail")
        assert (project / "app/service.py").read_text() == "old service"
        assert updates._version_in((project / "app/updates.py").read_text()) == "1.0.0"
        assert not (project / "app/brand_new.py").exists()
        assert not (project / "watchers/uber.yaml").exists()


def test_install_refuses_the_wrong_download_and_git_checkouts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(Path(tmp))
        github = FakeGitHub({f"{BASE}/tarball/abc123": FakeResponse(200, raw=_tarball(NEW_FILES))})
        try:
            updates.install(f"{BASE}/tarball/abc123", "2.0.0", REPO, project=project,
                            session=github, log=lambda _line: None)
        except updates.UpdateError as exc:
            assert "isn't Receipt Bridge 2.0.0" in str(exc)
        else:
            raise AssertionError("expected a version mismatch to be refused")
        assert (project / "app/service.py").read_text() == "old service"

        (project / ".git").mkdir()
        try:
            updates.install(f"{BASE}/tarball/abc123", "1.1.0", REPO, project=project, session=github)
        except updates.UpdateError as exc:
            assert "git pull" in str(exc)
        else:
            raise AssertionError("expected a git checkout to be left alone")


def _wait_for_check(service) -> None:
    for _ in range(100):
        if not service.update_snapshot()["checking"]:
            return
        time.sleep(0.02)
    raise AssertionError("update check never finished")


NEWER = {"version": "v99.0.0", "url": "https://github.com/o/r/releases/tag/v99.0.0",
         "notes": "", "published_at": None, "download": f"{BASE}/tarball/v99.0.0"}


def test_a_new_version_is_notified_once_and_shown_in_the_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        service.notify = mock.Mock()
        client, token = _client(service)
        headers = {"x-receipt-bridge": token}
        with mock.patch.object(updates, "latest", return_value=NEWER):
            for _ in range(2):
                assert client.post("/api/updates/check", headers=headers).json()["started"]
                _wait_for_check(service)
                time.sleep(0.05)      # the notification is posted just after
        service.notify.assert_called_once()
        assert "99.0.0 is available" in service.notify.call_args.args[1]

        state = client.get("/api/state", headers=headers).json()["update"]
        assert state["current"] == updates.VERSION
        assert state["latest"] == "v99.0.0" and state["available"]
        assert not service._update_check_due()          # checked: not asked again today

        with mock.patch("app.service.subprocess.run") as run:
            assert client.post("/api/updates/open", headers=headers).status_code == 200
        run.assert_called_once_with(["open", NEWER["url"]], check=False)


def test_update_now_installs_and_restarts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        service.db.set_state("update:last", json.dumps(NEWER))
        service.restart = mock.Mock(return_value=True)
        done = {"version": "99.0.0", "files": 3, "requirements": False, "app": False}
        with mock.patch.object(updates, "install", return_value=done) as install, \
                mock.patch.object(updates, "ROOT", Path(tmp)):          # not this checkout's own .git
            assert service.update_snapshot()["can_install"]
            service._run_update()
        install.assert_called_once()
        assert install.call_args.args[:2] == (NEWER["download"], "v99.0.0")
        service.restart.assert_called_once()
        assert service._outcome.ok and "Restarting" in service._outcome.message


def test_only_the_app_in_applications_rebuilds_itself() -> None:
    """A test copy (run from its own dist/) must never replace the real app."""
    from app import login_item

    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        service.db.set_state("update:last", json.dumps(NEWER))
        service.restart = mock.Mock()
        done = {"version": "99.0.0", "files": 3, "requirements": False, "app": True}
        for running, rebuilds in (("/tmp/test-copy/dist/Receipt Bridge.app", False), (None, False),
                                  (str(login_item.INSTALLED), True)):
            with mock.patch.object(updates, "install", return_value=done), \
                    mock.patch.object(login_item, "running_bundle", return_value=running), \
                    mock.patch.object(login_item, "install_app") as install_app:
                service._run_update()
            assert install_app.called == rebuilds, running


def test_when_it_cannot_restart_it_says_so() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        service.db.set_state("update:last", json.dumps(NEWER))
        service.restart = mock.Mock(return_value=False)
        done = {"version": "99.0.0", "files": 3, "requirements": False, "app": False}
        with mock.patch.object(updates, "install", return_value=done):
            service._run_update()
        assert service._outcome.ok and "Quit and reopen" in service._outcome.message


def test_a_terminal_python_is_not_mistaken_for_the_app() -> None:
    """The bundle is asked of macOS and checked by id: running here, from a
    terminal, there's no Receipt Bridge bundle to restart or rebuild."""
    from app import login_item

    assert login_item.running_bundle() is None


def test_a_failed_update_says_why_and_does_not_restart() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        service.db.set_state("update:last", json.dumps(NEWER))
        service.restart = mock.Mock()
        with mock.patch.object(updates, "install", side_effect=updates.UpdateError("GitHub is down.")):
            service._run_update()
        service.restart.assert_not_called()
        assert not service._outcome.ok and "GitHub is down." in service._outcome.message


def test_nothing_to_install_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        client, token = _client(service)
        same = dict(NEWER, version=f"v{updates.VERSION}")
        service.db.set_state("update:last", json.dumps(same))
        assert not service.update_snapshot()["available"]
        response = client.post("/api/updates/install", headers={"x-receipt-bridge": token})
        assert response.status_code == 400


def test_only_a_github_page_is_ever_opened() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp))
        service.db.set_state("update:last", '{"version": "v99", "url": "file:///etc/passwd"}')
        with mock.patch("app.service.subprocess.run") as run:
            assert not service.open_update_page()
        run.assert_not_called()


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
