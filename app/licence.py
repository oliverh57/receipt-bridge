"""The licence file: Receipt Bridge's Google and FreeAgent app keys, in one
file people are given and drop on the app.

The keys only identify the app (each person still signs in to their own
Gmail and FreeAgent), but they stay off GitHub so only people given the
file can connect. Installing it writes the two files the rest of the app
reads, `credentials.json` and `freeagent_credentials.json`, owner-only.

    .venv/bin/python -m app.licence make "Receipt Bridge.rbkey"

makes one from the two key files in this folder (after replacing a key,
make a new licence and hand that out).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SUFFIX = ".rbkey"
FORMAT = 1


class LicenceError(ValueError):
    pass


def parse(data: bytes) -> dict[str, Any]:
    """The keys in a licence file, checked. Raises LicenceError, in words."""
    try:
        licence = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LicenceError("That isn't a Receipt Bridge licence file.") from exc
    if not isinstance(licence, dict) or licence.get("receipt_bridge_licence") != FORMAT:
        raise LicenceError("That isn't a Receipt Bridge licence file.")
    google, freeagent = licence.get("google"), licence.get("freeagent")
    if google is not None:
        client = (google or {}).get("installed") or {}
        if not (client.get("client_id") and client.get("client_secret")):
            raise LicenceError("The licence file's Google key is incomplete. Ask for a new one.")
    if freeagent is not None:
        if not ((freeagent or {}).get("client_id") and freeagent.get("client_secret")):
            raise LicenceError("The licence file's FreeAgent key is incomplete. Ask for a new one.")
    if google is None and freeagent is None:
        raise LicenceError("The licence file has no keys in it. Ask for a new one.")
    return {"google": google, "freeagent": freeagent}


def install(data: bytes, google_path: Path, freeagent_path: Path) -> list[str]:
    """Save the keys in a licence file where the app reads them. Returns
    which were installed ("Google", "FreeAgent")."""
    keys = parse(data)
    installed = []
    for name, key, path in (("Google", keys["google"], google_path),
                            ("FreeAgent", keys["freeagent"], freeagent_path)):
        if key is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(key, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        installed.append(name)
    return installed


def make(google_path: Path, freeagent_path: Path) -> bytes:
    licence: dict[str, Any] = {"receipt_bridge_licence": FORMAT}
    for name, path in (("google", google_path), ("freeagent", freeagent_path)):
        if path.exists():
            licence[name] = json.loads(path.read_text(encoding="utf-8"))
    data = json.dumps(licence, indent=2).encode("utf-8")
    parse(data)                       # never hand out a file the app would refuse
    return data


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "make":
        sys.exit('usage: python -m app.licence make "Receipt Bridge.rbkey"')
    from .config import load_config

    config = load_config()
    out = Path(sys.argv[2])
    out.write_bytes(make(config.credentials_file, config.freeagent_credentials_file))
    os.chmod(out, 0o600)
    print(f"Wrote {out}. Give it only to people you trust; it isn't pushed to GitHub.")
