#!/usr/bin/env python3
"""Receipt Bridge command line.

    python cli.py auth                        sign in to Gmail (once)
    python cli.py serve                       start the review app
    python cli.py scan                        scan the inbox from the terminal
    python cli.py watchers                    list loaded watchers
    python cli.py test-watcher <id> <eml>     dry-run a watcher on a saved email
    python cli.py ingest <id> <eml>           full pipeline on a saved email
    python cli.py export <id> [<id>...]       export staged receipts
    python cli.py pending                     list what is waiting to be filed
    python cli.py freeagent-check <csv>       compare FreeAgent's transactions with a bank statement
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app.browser import BrowserPool
from app.config import load_config
from app.db import PENDING, Database
from app.email_message import Email
from app.export import export_receipts, reveal
from app.accounts import AccountStore, adopt_legacy_scan_state
from app.gmail_client import GmailAuthError
from app.pipeline import ScanReport, process_email, scan
from app.watchers import build_filename, load_watchers_safe


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
    )


def _watcher_by_id(config, watcher_id: str):
    watchers, errors = load_watchers_safe(config.watchers_dir)
    for problem in errors:
        print(f"warning: {problem}", file=sys.stderr)
    for watcher in watchers:
        if watcher.id == watcher_id:
            return watcher
    known = ", ".join(w.id for w in watchers) or "none"
    raise SystemExit(f"No watcher with id '{watcher_id}'. Loaded: {known}")


# ---- commands -----------------------------------------------------------


def cmd_auth(args, config) -> int:
    print("Opening a browser to authorise read-only Gmail access…")
    try:
        account = AccountStore(config).add_interactive()
    except GmailAuthError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    print(f"Signed in as {account.email}")
    print(f"Token cached at {account.token_path} (owner-only).")
    return 0


def cmd_serve(args, config) -> int:
    """Run the engine and API without the Mac window — for development.

    Open the printed URL in a browser. The Mac app is the normal way to use
    Receipt Bridge; this exists to work on the UI or run it headless.
    """
    import uvicorn

    from app.api import create_app
    from app.service import ReceiptService

    host = args.host or config.web.get("host", "127.0.0.1")
    port = args.port or int(config.web.get("port", 8765))

    service = ReceiptService(config)
    service.start()
    print(f"Receipt Bridge engine at http://{host}:{port}/  (Ctrl+C to stop)")
    uvicorn.run(create_app(service), host=host, port=port, log_level="warning")
    service.stop()
    return 0


def cmd_scan(args, config) -> int:
    db = Database(config.db_path)
    watchers, errors = load_watchers_safe(config.watchers_dir)
    for problem in errors:
        print(f"warning: {problem}", file=sys.stderr)

    store = AccountStore(config)
    adopt_legacy_scan_state(db, store)
    connected = store.clients()
    if not connected:
        print("No Gmail account connected. Run: python cli.py auth", file=sys.stderr)
        return 1

    report = scan(
        config,
        db,
        watchers,
        progress=print,
        accounts=[(a.email, c) for a, c in connected],
    )
    return 0 if report.failed == 0 else 1


def cmd_watchers(args, config) -> int:
    watchers, errors = load_watchers_safe(config.watchers_dir)
    for problem in errors:
        print(f"warning: {problem}", file=sys.stderr)
    if not watchers:
        print("No watchers loaded.")
        return 1
    for watcher in watchers:
        steps = ", ".join(
            str(s.get("fetcher") if isinstance(s, dict) else s) for s in watcher.pdf
        )
        print(f"{watcher.id:<16} {watcher.name}")
        print(f"{'':<16} query : {watcher.gmail_query}")
        print(f"{'':<16} pdf   : {steps}")
        print(f"{'':<16} file  : {watcher.filename}")
    return 0


def cmd_test_watcher(args, config) -> int:
    """Show what a watcher pulls out of a saved email. No network, no PDF."""
    watcher = _watcher_by_id(config, args.watcher_id)
    message = Email.from_eml(args.eml)

    print(f"Subject : {message.subject}")
    print(f"From    : {message.sender}")
    print(f"Matches : {watcher.matches(message)}")
    if not watcher.matches(message):
        print("\nThe match: block rejected this email — nothing else will run.")
        return 1

    values = watcher.extract(message)
    print("\nExtracted:")
    width = max(len(k) for k in values)
    for key, value in values.items():
        shown = value if len(str(value)) < 90 else str(value)[:87] + "…"
        print(f"  {key:<{width}} : {shown}")

    print(f"\nFilename: {build_filename(watcher.filename, values)}")

    if args.dump_text:
        print("\n--- flattened email text ---")
        print(message.text)
    return 0


def cmd_ingest(args, config) -> int:
    """Run the whole pipeline against a local .eml, including the PDF step."""
    config.ensure_dirs()
    watcher = _watcher_by_id(config, args.watcher_id)
    message = Email.from_eml(args.eml)
    if not message.message_id:
        message.message_id = f"local:{Path(args.eml).name}"

    db = Database(config.db_path)
    report = ScanReport()
    with BrowserPool(
        headless=config.headless,
        block_trackers=bool(config.pdf.get("block_trackers", True)),
    ) as pool:
        receipt_id = process_email(config, db, watcher, message, pool, report)

    # report.notes are already emitted through logging; don't print them twice.
    if receipt_id is None:
        print("Nothing staged.")
        return 1

    record = db.get_receipt(receipt_id)
    print(f"\nStaged #{receipt_id}")
    print(f"  file   : {record['filename']}")
    print(f"  source : {record['pdf_source']}")
    print(f"  pdf    : {record['pdf_path']}")
    return 0


def cmd_pending(args, config) -> int:
    db = Database(config.db_path)
    rows = db.list_receipts(PENDING)
    if not rows:
        print("Nothing staged.")
        return 0
    for row in rows:
        total = f"{row['total']:.2f}" if row["total"] is not None else "—"
        print(
            f"#{row['id']:<4} {row['purchased_on'] or row['email_date']}  "
            f"{row['currency'] or '':<3} {total:>9}  {row['vendor']:<12} "
            f"{row['filename']}"
        )
    return 0


def cmd_export(args, config) -> int:
    db = Database(config.db_path)
    ids = args.ids or [row["id"] for row in db.list_receipts(PENDING)]
    if not ids:
        print("Nothing to export.")
        return 0

    result = export_receipts(config, db, ids)
    print(f"Exported {result.count} receipt(s) to {result.folder}")
    for name in result.exported:
        print(f"  {name}")
    for problem in result.missing:
        print(f"  skipped: {problem}", file=sys.stderr)
    if not args.no_open:
        reveal(result.folder)
    return 0


def cmd_app(args, config) -> int:
    """Run the native macOS app without building a bundle."""
    from app.mac_app import main as run_app

    run_app()
    return 0


def cmd_reset(args, config) -> int:
    """Clear staged receipts so the next scan starts from scratch.

    Deliberately leaves Gmail credentials alone — re-authorising is a chore,
    and nothing here is derived from them. Everything removed is rebuilt by
    the next scan, since the inbox, not this database, is the source of truth.
    Exported batches are left alone too: once filed, they are yours.
    """
    import shutil

    from app.db import Database

    targets = [
        ("staged receipts database", config.db_path),
        ("generated PDFs", config.pdf_dir),
        ("failure screenshots", config.debug_dir),
    ]
    present = [(label, path) for label, path in targets if path.exists()]

    if not present:
        print("Nothing to clear — already empty.")
        return 0

    db = Database(config.db_path) if config.db_path.exists() else None
    staged = len(db.list_receipts()) if db else 0

    print("This will delete:")
    for label, path in present:
        print(f"  - {label}: {path}")
    print(f"\n{staged} staged receipt(s) will be forgotten and re-collected")
    print("on the next scan. Gmail sign-in and exported batches are kept.")

    if not args.yes:
        reply = input("\nType 'clear' to confirm: ").strip().lower()
        if reply != "clear":
            print("Cancelled.")
            return 1

    for label, path in present:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        print(f"  removed {label}")

    config.ensure_dirs()
    print("\nCleared. Run a scan to rebuild from the inbox.")
    return 0


def cmd_config(args, config) -> int:
    print(json.dumps(config.raw, indent=2))
    print(f"\ndatabase : {config.db_path}")
    print(f"staging  : {config.pdf_dir}")
    print(f"exports  : {config.export_dir}")
    print(f"watchers : {config.watchers_dir}")
    return 0


def cmd_freeagent_check(args, config) -> int:
    """Check what the app has read from FreeAgent against a statement CSV
    (date, amount, description per line), then show how receipts pair up.
    Reads the app's copy of the transactions: press Refresh in Settings
    (or let the hourly read run) first. Never writes to FreeAgent."""
    import csv
    from datetime import datetime

    from app.matcher import match_receipts

    db = Database(config.db_path)
    try:
        chosen = json.loads(db.get_state("freeagent:accounts") or "[]")
        reference = json.loads(db.get_state("freeagent:reference") or "{}")
    except ValueError:
        chosen, reference = [], {}
    if not chosen:
        print("No business bank account chosen yet: connect FreeAgent and tick one in Settings.")
        return 1
    synced = [dict(t) for t in db.bank_transactions(chosen)]
    print(f"FreeAgent ({reference.get('environment', '?')}): {len(synced)} transaction(s) in "
          f"{len(chosen)} chosen account(s), last read {db.get_state('freeagent:last_sync') or 'never'}")

    def day(value: str) -> str:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.strip(), fmt).date().isoformat()
            except ValueError:
                pass
        raise ValueError(f"unrecognised date {value!r}")

    expected = []
    with open(args.csv, newline="", encoding="utf-8") as handle:
        for line in csv.reader(handle):
            if len(line) >= 3 and line[0].strip():
                expected.append((day(line[0]), round(float(line[1]), 2), line[2].strip()))

    unused = list(synced)
    missing = []
    for when, amount, description in expected:
        hit = next((t for t in unused if t["dated_on"] == when and round(t["amount"], 2) == amount
                    and description.upper()[:6] in (t["description"] or "").upper()), None)
        if hit:
            unused.remove(hit)
        else:
            missing.append((when, amount, description))
    print(f"Statement: {len(expected)} line(s); {len(expected) - len(missing)} found in FreeAgent")
    for when, amount, description in missing:
        print(f"  MISSING  {when}  {amount:9.2f}  {description}")
    for t in unused:
        print(f"  EXTRA    {t['dated_on']}  {t['amount']:9.2f}  {t['description']}")

    if expected:
        first, last = min(e[0] for e in expected), max(e[0] for e in expected)
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM receipts WHERE status IN ('pending', 'exported') "
                "AND purchased_on BETWEEN date(?, '-7 days') AND ? ORDER BY purchased_on, id",
                (first, last)).fetchall()
        currencies = {a.get("currency", "GBP") for a in reference.get("bank_accounts", []) if a["url"] in chosen}
        receipts = [{"id": r["id"], "date": r["purchased_on"], "total": r["total"], "currency": r["currency"],
                     "vendor": r["vendor"], "paid_by": r["paid_by"]} for r in rows]
        results = match_receipts(receipts, synced, currencies or {"GBP"})
        print(f"\nReceipts from {first} (less a week) to {last}: {len(receipts)}")
        counts: dict[str, int] = {}
        for r in receipts:
            m = results[r["id"]]
            counts[m.status] = counts.get(m.status, 0) + 1
            t = m.transaction
            shown = f"{t['dated_on']}  {t['description']}" if t else m.reason
            print(f"  {r['date']}  {(r['vendor'] or '')[:14]:14} {r['total'] or 0:8.2f}  {m.status:9} {shown}")
        print("  " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0 if not missing else 2


# ---- entry point --------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Collect receipt emails and batch them up for FreeAgent.",
    )
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth", help="sign in to Gmail (read-only)")

    serve = subparsers.add_parser("serve", help="run the engine and API without the Mac window")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    subparsers.add_parser("scan", help="scan the inbox now")
    subparsers.add_parser("watchers", help="list loaded watchers")
    subparsers.add_parser("pending", help="list staged receipts")
    subparsers.add_parser("config", help="show resolved configuration")
    subparsers.add_parser("app", help="run the native macOS app")

    reset = subparsers.add_parser(
        "reset", help="clear staged receipts and start over (keeps sign-in)"
    )
    reset.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )

    test = subparsers.add_parser(
        "test-watcher", help="dry-run a watcher against a saved .eml"
    )
    test.add_argument("watcher_id")
    test.add_argument("eml")
    test.add_argument(
        "--dump-text",
        action="store_true",
        help="print the flattened email text your patterns run against",
    )

    ingest = subparsers.add_parser(
        "ingest", help="run the full pipeline on a saved .eml"
    )
    ingest.add_argument("watcher_id")
    ingest.add_argument("eml")

    check = subparsers.add_parser(
        "freeagent-check", help="compare FreeAgent's transactions with a statement CSV")
    check.add_argument("csv", help="lines of date, amount, description")

    export = subparsers.add_parser("export", help="export staged receipts")
    export.add_argument("ids", nargs="*", type=int, help="defaults to everything staged")
    export.add_argument("--no-open", action="store_true")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    config = load_config(args.config)

    handlers = {
        "auth": cmd_auth,
        "serve": cmd_serve,
        "scan": cmd_scan,
        "watchers": cmd_watchers,
        "test-watcher": cmd_test_watcher,
        "ingest": cmd_ingest,
        "pending": cmd_pending,
        "export": cmd_export,
        "config": cmd_config,
        "reset": cmd_reset,
        "app": cmd_app,
        "freeagent-check": cmd_freeagent_check,
    }
    return handlers[args.command](args, config)


if __name__ == "__main__":
    sys.exit(main())
