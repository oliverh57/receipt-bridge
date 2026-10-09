"""Is there a newer Receipt Bridge? Asks GitHub, and installs it.

`VERSION` is this copy's version, and the one place it is written down:
build_app.py stamps it into the app's Info.plist. To ship an update, raise
it and push: the check reads this file on GitHub's default branch. A GitHub
release of the same version is optional; if there is one, its notes show as
"What's new". Anything higher than `VERSION` is an update.

Installing downloads that version's source and copies it over this folder,
which is where the app runs from (build_app.py). Everything that belongs to
this Mac stays as it is: `data/` (receipts, database, sign-ins), the
virtualenv, `config.yaml`, the credential files, and the supplier rules in
`watchers/` (new ones are added; ones already here may have been edited in
Settings). The code being replaced is kept in `data/updates/` first, and put
back if the install fails part-way.

A git checkout is never copied over: it updates with `git pull --ff-only`,
which never merges and won't overwrite edits not committed yet. Either way,
when the files here are already newer than what's running, updating is just
a restart.
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import requests

VERSION = "1.1.16"
DEFAULT_REPO = "oliverh57/receipt-bridge"
API = "https://api.github.com"
USER_AGENT = f"Receipt Bridge/{VERSION}"
TIMEOUT = 15
ROOT = Path(__file__).resolve().parent.parent

# Never touched by an update (folders, and their contents).
SKIP = {"data", ".venv", "backups", "dist", "build", ".git", ".claude"}
# Added if missing, never replaced: they belong to this Mac.
KEEP = {"config.yaml", "credentials.json", "freeagent_credentials.json", "token.json", ".env"}
KEEP_FOLDERS = {"watchers"}


class UpdateError(RuntimeError):
    pass


def parse(tag: str | None) -> tuple[int, ...] | None:
    """`v1.2` → (1, 2, 0). Anything after the numbers (`-beta`) is ignored;
    a tag with no leading number isn't a version."""
    found = re.match(r"\s*v?(\d+(?:\.\d+)*)", tag or "", re.IGNORECASE)
    if not found:
        return None
    parts = [int(p) for p in found.group(1).split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def is_newer(latest: str, current: str | None = None) -> bool:
    """Is `latest` higher than `current` (by default, this running copy)?"""
    theirs, ours = parse(latest), parse(VERSION if current is None else current)
    return theirs is not None and ours is not None and theirs > ours


def _version_in(source: str) -> str | None:
    found = re.search(r'^VERSION = "([^"]+)"', source, re.M)
    return found.group(1) if found else None


class GitHub:
    def __init__(self, repo: str, session: Any = None):
        self.repo = repo
        self.http = session or requests

    def get(self, path: str, accept: str = "application/vnd.github+json", **kwargs: Any) -> Any:
        url = path if path.startswith("https://") else f"{API}/repos/{self.repo}/{path}".rstrip("/")
        try:
            return self.http.get(url, headers={"Accept": accept, "User-Agent": USER_AGENT},
                                 timeout=kwargs.pop("timeout", TIMEOUT), **kwargs)
        except requests.RequestException as exc:
            raise UpdateError("Couldn't reach GitHub. Check the internet connection.") from exc

    def fail(self, response: Any) -> UpdateError:
        if response.status_code == 404:
            return UpdateError(f"Couldn't find {self.repo} on GitHub. If it's private, GitHub won't show it.")
        if response.status_code in (403, 429):
            return UpdateError("GitHub is limiting requests. Try again in an hour.")
        return UpdateError(f"GitHub answered {response.status_code}.")


# Lines in a commit message that are for git, not for people: "Co-Authored-By:",
# "Signed-off-by:", "Claude-Session:" and the like. Never shown as What's new.
_TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*(?:-By|-by|-Session|-Id|-ID):\s*\S.*$")


def notes_from(message: str) -> str:
    """A commit message or release body as What's new: without the trailer
    lines git tools add, and without the blank lines they leave behind."""
    lines = [line for line in (message or "").splitlines() if not _TRAILER.match(line.strip())]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def latest(repo: str = DEFAULT_REPO, session: Any = None) -> dict[str, Any]:
    """The newest version on GitHub: its number, a page about it, notes,
    and where to download it. The version on the default branch, so pushing
    is enough; a release with the same or a higher version wins, for its
    notes."""
    github = GitHub(repo, session)
    info = github.get("")
    if info.status_code != 200:
        raise github.fail(info)
    branch = info.json().get("default_branch") or "main"
    commit = github.get(f"commits/{branch}")
    if commit.status_code != 200:
        raise github.fail(commit)
    sha = commit.json()["sha"]
    source = github.get(f"contents/app/updates.py?ref={sha}")
    if source.status_code != 200:
        raise github.fail(source)
    content = source.json().get("content") or ""
    version = _version_in(base64.b64decode(content).decode("utf-8", "replace"))
    if not version:
        raise UpdateError(f"{repo} on GitHub doesn't say which version it is.")
    details = commit.json().get("commit") or {}
    pushed = {"version": version,
              "url": f"https://github.com/{repo}/commits/{branch}",
              "notes": notes_from(details.get("message") or ""),
              "published_at": (details.get("committer") or {}).get("date"),
              "download": f"{API}/repos/{repo}/tarball/{sha}"}   # that commit, not whatever's newest later

    response = github.get("releases/latest")          # 404 when there are none
    if response.status_code != 200:
        return pushed
    release = response.json()
    tag = release.get("tag_name") or ""
    if not parse(tag) or parse(tag) < parse(version):
        return pushed
    return {"version": tag,
            "url": release.get("html_url") or f"https://github.com/{repo}/releases",
            "notes": notes_from(release.get("body") or ""),
            "published_at": release.get("published_at"),
            "download": release.get("tarball_url") or f"{API}/repos/{repo}/tarball/{tag}"}


def _wanted(rel: Path) -> bool:
    return (rel.parts[0] not in SKIP and "__pycache__" not in rel.parts
            and rel.suffix != ".pyc" and rel.name != ".DS_Store")


def _replaceable(rel: Path, project: Path) -> bool:
    """False for this Mac's own files that already exist here."""
    mine = rel.as_posix() in KEEP or rel.parts[0] in KEEP_FOLDERS
    return not (mine and (project / rel).exists())


def install(download: str, expected: str, repo: str = DEFAULT_REPO, project: Path = ROOT,
            session: Any = None, log: Any = print) -> dict[str, Any]:
    """Download a version and put it in place of this one. Returns what
    changed: {version, files, requirements, app}. The caller restarts."""
    if (project / ".git").exists():
        raise UpdateError("This copy is a git checkout. Update it with git pull.")
    work = project / "data" / "updates"
    work.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=work) as tmp:
        tmp_path = Path(tmp)
        log(f"Downloading {expected}")
        response = GitHub(repo, session).get(download, accept="application/vnd.github+json",
                                             stream=True, timeout=120)
        if response.status_code != 200:
            raise GitHub(repo).fail(response)
        archive = tmp_path / "update.tar.gz"
        with archive.open("wb") as handle:
            for chunk in response.iter_content(1 << 16):
                handle.write(chunk)

        unpacked = tmp_path / "new"
        try:
            with tarfile.open(archive) as tar:
                tar.extractall(unpacked, filter="data")     # refuses paths outside, links out, devices
        except (tarfile.TarError, OSError) as exc:
            raise UpdateError(f"The download from GitHub was damaged: {exc}") from exc
        tops = [p for p in unpacked.iterdir() if p.is_dir()]
        new = tops[0] if len(tops) == 1 else unpacked     # GitHub wraps it in owner-repo-sha/
        found = _version_in((new / "app" / "updates.py").read_text(encoding="utf-8")) \
            if (new / "app" / "updates.py").is_file() else None
        if found is None or parse(found) != parse(expected):
            raise UpdateError(f"The download isn't Receipt Bridge {expected} (it says {found or 'nothing'}).")

        files = [p.relative_to(new) for p in new.rglob("*") if p.is_file()]
        files = [rel for rel in files if _wanted(rel) and _replaceable(rel, project)]
        changed = [rel for rel in files
                   if not (project / rel).is_file() or _digest(project / rel) != _digest(new / rel)]
        if not changed:
            return {"version": found, "files": 0, "requirements": False, "app": False}

        backup = work / f"before-{found}.tar.gz"
        with tarfile.open(backup, "w:gz") as tar:
            for rel in changed:
                if (project / rel).is_file():
                    tar.add(project / rel, arcname=rel.as_posix())
        added = [rel for rel in changed if not (project / rel).exists()]

        log(f"Installing {len(changed)} changed file{'' if len(changed) == 1 else 's'}")
        try:
            for rel in changed:
                (project / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(new / rel, project / rel)
        except OSError as exc:
            _restore(backup, added, project)
            raise UpdateError(f"Couldn't install the update, so nothing changed: {exc}") from exc

    names = {rel.as_posix() for rel in changed}
    if "requirements.txt" in names and not _install_requirements(project, log):
        _restore(backup, added, project)
        raise UpdateError("Couldn't download what the new version needs, so nothing changed. "
                          "Check the internet connection and try again.")
    return _changes(found, names)


def pull(project: Path = ROOT, log: Any = print) -> dict[str, Any]:
    """Update a git checkout from GitHub: fast-forward only, so it never
    merges, and git refuses rather than overwrite edits not committed yet."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, timeout=120)

    before = git("rev-parse", "HEAD").stdout.strip()
    log("Pulling from GitHub")
    result = git("pull", "--ff-only")
    if result.returncode != 0:
        why = (result.stderr.strip().splitlines() or ["git pull failed"])[-1]
        raise UpdateError(f"git pull couldn't update this copy, so nothing changed: {why}")
    after = git("rev-parse", "HEAD").stdout.strip()
    names = set(git("diff", "--name-only", before, after).stdout.split()) if before != after else set()
    if "requirements.txt" in names and not _install_requirements(project, log):
        raise UpdateError("Updated, but couldn't download what the new version needs. "
                          "Check the internet connection, then run Install Receipt Bridge.command.")
    return _changes(on_disk_version(project), names)


def on_disk_version(project: Path = ROOT) -> str | None:
    """The version the files here are now: ahead of VERSION once an update
    is installed (or pulled) and the app hasn't restarted yet."""
    try:
        return _version_in((project / "app" / "updates.py").read_text(encoding="utf-8"))
    except OSError:
        return None


def _changes(version: str | None, names: set[str]) -> dict[str, Any]:
    return {"version": version, "files": len(names), "requirements": "requirements.txt" in names,
            "app": bool(names & {"build_app.py", "app/login_item.py"})}


def _install_requirements(project: Path, log: Any) -> bool:
    log("Installing what the new version needs")
    python = project / ".venv" / "bin" / "python"
    steps = [[str(python), "-m", "pip", "install", "--quiet", "-r", str(project / "requirements.txt")],
             [str(python), "-m", "playwright", "install", "chromium"]]
    return all(subprocess.run(step, cwd=project, capture_output=True, text=True, timeout=900).returncode == 0
               for step in steps)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _restore(backup: Path, added: list[Path], project: Path) -> None:
    """Undo a half-finished install: the old files back, the new ones gone."""
    with tarfile.open(backup) as tar:
        tar.extractall(project, filter="data")
    for rel in added:
        (project / rel).unlink(missing_ok=True)


def relaunch_after_exit(pid: int, bundle: str) -> None:
    """Open the app again once this process has gone. Detached, so it
    outlives the quit."""
    script = f'while kill -0 {int(pid)} 2>/dev/null; do sleep 0.3; done; sleep 0.5; open "$0"'
    subprocess.Popen(["/bin/sh", "-c", script, bundle], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":       # `python -m app.updates`: what would happen, from a terminal
    found = latest(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPO)
    print(f"This copy: {VERSION}. GitHub: {found['version']}"
          f" ({'update available' if is_newer(found['version']) else 'up to date'}).")
