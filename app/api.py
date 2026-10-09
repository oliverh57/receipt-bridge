"""HTTP API and the single-page UI it serves.

Every endpoint here is fast by construction: it reads the service's in-memory
snapshot or the local database, or it *queues* slow work and returns at once.
Nothing in a request handler talks to Gmail or a supplier's website.

Security. The server binds to 127.0.0.1, but that alone is not enough: any web
page open in any browser on this Mac can send requests to 127.0.0.1. The
previous version accepted plain form posts, so a hostile page could have
signed the user out or exported their receipts. Two defences now:

* Every state-changing call must carry a per-launch secret in a header. A
  foreign page cannot read the secret (same-origin policy) and cannot set a
  custom header cross-origin without a CORS preflight, which this server
  never approves.
* Requests whose Host is not localhost are refused, which stops DNS
  rebinding attacks from impersonating the local origin.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import secrets
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import EXPORTED, FAILED, IGNORED, PENDING
from .freeagent import VAT_SCHEMES, FreeAgentError
from .service import ReceiptService

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).parent / "ui"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class Ids(BaseModel):
    ids: list[int]


class StatusChange(BaseModel):
    ids: list[int]
    status: str


class TidyChange(BaseModel):
    on: bool


class Connect(BaseModel):
    scan_from: str | None = None     # first scan of the new mailbox starts here


class Email(BaseModel):
    email: str


class Settings(BaseModel):
    auto_scan: bool | None = None
    theme: str | None = None
    waiting_days: int | None = None
    archive_delete_days: int | None = None
    open_at_login: bool | None = None
    notify_enabled: bool | None = None
    notify_frequency: str | None = None
    notify_categories: dict[str, bool] | None = None


class ArchivedDelete(BaseModel):
    ids: list[int] | None = None
    all: bool = False


class EmailIds(BaseModel):
    ids: list[str]


class EmailAdd(BaseModel):
    account: str = ""
    supplier: str = ""
    date: str = ""
    total: float | str | None = None
    currency: str = "GBP"
    vat: float | str | None = None
    vat_choice: str = "auto"
    paid_by: str = "business"


class SupplierSearch(BaseModel):
    q: str


class SupplierSample(BaseModel):
    message_id: str
    account: str = ""
    choices: dict[str, Any] = {}


def _callback_page(message: str) -> str:
    import html
    return ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Receipt Bridge</title>"
            "<body style=\"font:16px -apple-system,sans-serif;max-width:32em;margin:15vh auto;padding:0 16px\">"
            f"<h1 style=\"font-size:20px\">Receipt Bridge</h1><p>{html.escape(message)}</p></body></html>")


def create_app(service: ReceiptService) -> FastAPI:
    app = FastAPI(title="Receipt Bridge", docs_url=None, redoc_url=None, openapi_url=None)
    token = secrets.token_urlsafe(24)
    app.state.token = token

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in LOCAL_HOSTS:
            return JSONResponse({"error": "forbidden host"}, status_code=403)

        path = request.url.path
        if path.startswith("/api/"):
            supplied = request.headers.get("x-receipt-bridge") or request.query_params.get("t")
            # The query form exists only for GETs that cannot set headers —
            # the PDF in an <iframe>. It never authorises a change.
            if request.method != "GET" and request.headers.get("x-receipt-bridge") != token:
                return JSONResponse({"error": "missing token"}, status_code=403)
            if request.method == "GET" and supplied != token:
                return JSONResponse({"error": "missing token"}, status_code=403)

        response = await call_next(request)
        # The UI is never meant to be framed by anything but itself.
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Cache-Control"] = "no-store"
        return response

    # ---- UI -------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")

    @app.get("/bank-logo/{key}")
    def bank_logo(key: str):
        """A bank's own icon, fetched to data/bank-logos by tools/fetch_bank_logos.py.
        Never shipped with the app; a missing one makes the UI fall back to a monogram."""
        folder = service.config.data_dir / "bank-logos"
        if re.fullmatch(r"[a-z0-9]+", key):
            for ext in (".svg", ".png", ".ico", ".jpg"):
                if (folder / (key + ext)).is_file():
                    return FileResponse(folder / (key + ext), headers={"Cache-Control": "max-age=86400"})
        raise HTTPException(404)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("__TOKEN__", token)
        # Set the chosen theme in the markup itself, so the first paint is
        # already right rather than corrected a moment later by script.
        theme = service.theme
        if theme in ("light", "dark"):
            html = html.replace('<html lang="en">', f'<html lang="en" data-theme="{theme}">', 1)
        return HTMLResponse(html)

    # ---- state ----------------------------------------------------------

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return service.snapshot()

    @app.get("/api/receipts")
    def receipts(status: str = PENDING) -> list[dict[str, Any]]:
        try:
            return service.receipts(status)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/receipts/{receipt_id}/page/{page}")
    def page_image(receipt_id: int, page: int) -> FileResponse:
        """One page of a PDF receipt as an image, to draw highlights on."""
        path = service.page_image(receipt_id, page)
        if path is None:
            raise HTTPException(404, "no such page")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/receipts/{receipt_id}/original")
    def original(receipt_id: int) -> FileResponse:
        """A photo exactly as it arrived, before any tidying."""
        path = service.original_path(receipt_id)
        if path is None:
            raise HTTPException(404, "No original photo for that receipt")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                            headers={"Content-Disposition": "inline"})

    @app.post("/api/receipts/{receipt_id}/tidy")
    def set_tidy(receipt_id: int, change: TidyChange) -> dict[str, Any]:
        try:
            service.set_tidy(receipt_id, change.on)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/receipts/{receipt_id}/pdf")
    def pdf(receipt_id: int) -> FileResponse:
        path = service.pdf_path(receipt_id)
        if path is None:
            raise HTTPException(404, "No document for that receipt")
        return FileResponse(
            path,
            media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            headers={"Content-Disposition": "inline"},
        )

    @app.get("/api/supplier-names")
    def supplier_names() -> list[str]:
        return service.supplier_names()

    @app.get("/api/suppliers")
    def suppliers() -> list[dict[str, Any]]:
        return service.suppliers()

    # ---- actions --------------------------------------------------------

    @app.post("/api/check-now")
    def check_now() -> dict[str, Any]:
        return {"queued": service.check_now()}

    @app.post("/api/scan")
    def scan() -> dict[str, Any]:
        return {"queued": service.scan()}

    @app.post("/api/receipts/{receipt_id}/retry")
    def retry(receipt_id: int) -> dict[str, Any]:
        return {"queued": service.retry(receipt_id)}

    @app.post("/api/receipts/status")
    def set_status(change: StatusChange) -> dict[str, Any]:
        try:
            service.set_status(change.ids, change.status)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/archived/delete")
    def delete_archived(body: ArchivedDelete) -> dict[str, Any]:
        """Delete archived receipts for good: these ids, or every one."""
        if body.ids is None and not body.all:
            raise HTTPException(400, "Choose receipts to delete, or all")
        return {"deleted": service.delete_archived(None if body.all else body.ids)}

    @app.post("/api/export")
    def export(body: Ids) -> dict[str, Any]:
        if not body.ids:
            raise HTTPException(400, "Choose at least one receipt")
        result = service.export(body.ids)
        return {
            "folder": str(result.folder),
            "name": result.folder.name,
            "count": result.count,
            "missing": result.missing,
        }

    @app.post("/api/reveal")
    def reveal(body: dict[str, str]) -> dict[str, Any]:
        """Show an export folder in Finder.

        Confined to the export directory: this endpoint must not become a way
        to open arbitrary paths on the machine.
        """
        root = service.config.export_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = (root / body.get("name", "")).resolve()
        if root != target and root not in target.parents:
            raise HTTPException(400, "Not an export folder")
        if not target.exists():
            target = root
        subprocess.run(["open", str(target)], check=False)
        return {"ok": True}

    @app.post("/api/accounts/connect")
    def connect(body: Connect | None = None) -> dict[str, Any]:
        if not service.config.credentials_file.exists():
            raise HTTPException(400, "This copy is missing its Google key (credentials.json). "
                                     "Update from Settings → General, or run the installer again.")
        scan_from = None
        if body and body.scan_from:
            try:
                scan_from = date.fromisoformat(body.scan_from[:10])
            except ValueError:
                raise HTTPException(400, "scan_from must be a date (YYYY-MM-DD)")
            if scan_from > date.today():
                raise HTTPException(400, "scan_from can't be in the future")
        return {"started": service.connect_account(scan_from)}

    @app.post("/api/accounts/connect/cancel")
    def cancel_connect() -> dict[str, Any]:
        return {"cancelled": service.cancel_connect()}

    @app.post("/api/accounts/disconnect")
    def disconnect(body: Email) -> dict[str, Any]:
        return {"removed": service.disconnect_account(body.email)}

    @app.post("/api/accounts/check")
    def check() -> dict[str, Any]:
        return {"queued": service.check_accounts()}

    # ---- Emails: browse the mailbox, add any one email to Files ---------
    #
    # Like the supplier search, the list and an opened email are read from
    # Gmail while the page waits (see ReceiptService.list_emails); adding
    # one is queued.

    def gmail_failed(exc: Exception) -> HTTPException:
        from .gmail_client import GmailAuthError

        text = str(exc)
        if isinstance(exc, GmailAuthError) or "invalid_grant" in text or "expired or revoked" in text:
            return HTTPException(400, "Gmail sign-in has expired. Reconnect the account.")
        log.info("emails: %s", text)
        return HTTPException(502, f"Couldn't reach Gmail: {text[:200]}")

    @app.get("/api/emails")
    def emails(account: str = "", q: str = "", receipts: bool = False, page: str = "") -> dict[str, Any]:
        try:
            return service.list_emails(account, q[:500], receipts, page)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise gmail_failed(exc) from exc

    @app.post("/api/emails/known")
    def emails_known(body: EmailIds) -> dict[str, Any]:
        return service.emails_in_files(body.ids)

    def gmail_id(message_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", message_id):
            raise HTTPException(400, "Not a Gmail message id")
        return message_id

    @app.get("/api/emails/{message_id}")
    def email_open(message_id: str, account: str = "") -> dict[str, Any]:
        gmail_id(message_id)
        try:
            return service.open_email(account, message_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise gmail_failed(exc) from exc

    @app.get("/api/emails/{message_id}/pdf")
    def email_pdf(message_id: str, account: str = "") -> Response:
        """The email's attached PDF, for the preview in "Convert to receipt"
        (an <iframe>, so the token comes as ?t=)."""
        gmail_id(message_id)
        try:
            found = service.email_pdf(account, message_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise gmail_failed(exc) from exc
        if found is None:
            raise HTTPException(404, "This email has no PDF attached")
        return Response(found[1], media_type="application/pdf", headers={"Content-Disposition": "inline"})

    @app.post("/api/emails/{message_id}/add")
    def email_add(message_id: str, body: EmailAdd) -> dict[str, Any]:
        gmail_id(message_id)
        try:
            return {"queued": service.add_email(body.account, message_id, body.model_dump())}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ---- FreeAgent ------------------------------------------------------
    #
    # The sign-in redirect lands on this server, so it works while the app
    # is running. The callback is outside /api/: FreeAgent's redirect can't
    # carry the per-launch token. It's protected instead by the OAuth
    # `state`, which must be one this app issued in the last ten minutes,
    # and is accepted once. The Host check above still applies.

    @app.post("/api/freeagent/connect")
    def freeagent_connect() -> dict[str, Any]:
        try:
            return {"url": service.connect_freeagent()}
        except FreeAgentError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/freeagent/callback", response_class=HTMLResponse)
    def freeagent_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
        if error or not service.freeagent_callback(code, state):
            message = ("FreeAgent sign-in was cancelled." if error else
                       "This sign-in link has expired or was already used. Start again from Receipt Bridge's Settings.")
            return HTMLResponse(_callback_page(message), status_code=400)
        return HTMLResponse(_callback_page("FreeAgent is connected. You can close this tab and go back to Receipt Bridge."))

    @app.post("/api/freeagent/disconnect")
    def freeagent_disconnect() -> dict[str, Any]:
        service.disconnect_freeagent()
        return {"ok": True}

    @app.post("/api/freeagent/accounts")
    def freeagent_accounts(body: dict[str, Any]) -> dict[str, Any]:
        urls = body.get("urls")
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            raise HTTPException(400, "urls must be a list")
        service.set_freeagent_accounts(urls)
        return {"ok": True}

    @app.get("/api/freeagent/categories")
    def freeagent_categories() -> list[dict[str, Any]]:
        return service._freeagent_reference().get("categories", [])

    @app.post("/api/freeagent/dry-run")
    def freeagent_dry_run(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body.get("on"), bool):
            raise HTTPException(400, "on must be true or false")
        service.set_freeagent_dry_run(body["on"])
        return {"ok": True}

    @app.post("/api/freeagent/vat-scheme")
    def freeagent_vat_scheme(body: dict[str, Any]) -> dict[str, Any]:
        scheme = body.get("scheme")
        if scheme != "" and scheme not in VAT_SCHEMES:
            raise HTTPException(400, "scheme must be one of " + ", ".join(VAT_SCHEMES) + ", or empty")
        service.set_vat_scheme(scheme)
        return {"ok": True}

    @app.post("/api/receipts/{receipt_id}/fields")
    def receipt_fields(receipt_id: int, body: dict[str, Any]) -> dict[str, Any]:
        try:
            service.set_receipt_fields(receipt_id, body)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/receipts/file")
    def file_receipts(body: Ids) -> dict[str, Any]:
        return {"queued": service.file(body.ids)}

    @app.post("/api/receipts/unfile")
    def unfile_many(body: Ids) -> dict[str, Any]:
        return {"queued": service.unfile_many(body.ids)}

    @app.post("/api/receipts/{receipt_id}/payment")
    def pin_payment(receipt_id: int, body: dict[str, Any]) -> dict[str, Any]:
        try:
            service.set_payment(receipt_id, body.get("url") or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/receipts/{receipt_id}/remove-from-payment")
    def remove_from_payment(receipt_id: int, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body.get("url"), str):
            raise HTTPException(400, "url is required")
        try:
            service.remove_file_from_payment(receipt_id, body["url"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/statement")
    def statement(account: str = "", month: str = "") -> dict[str, Any]:
        if month and not re.fullmatch(r"\d{4}-\d{2}", month):
            raise HTTPException(400, "month must be YYYY-MM")
        return service.statement(account or None, month or None)

    @app.post("/api/statement/payment")
    def statement_payment(body: dict[str, Any]) -> dict[str, Any]:
        """A payment's category / VAT rate / re-billing (Statement)."""
        if not isinstance(body.get("url"), str):
            raise HTTPException(400, "url is required")
        try:
            return {"settings": service.set_payment_settings(body["url"], body.get("changes") or {})}
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/statement/explain")
    def statement_explain(body: dict[str, Any]) -> dict[str, Any]:
        """"No receipt needed" with a category: explain it in FreeAgent."""
        if not isinstance(body.get("url"), str):
            raise HTTPException(400, "url is required")
        return {"queued": service.explain_payment(body["url"], body.get("reason") or None)}

    @app.post("/api/statement/payment-reset")
    def statement_payment_reset(body: dict[str, Any]) -> dict[str, Any]:
        """Forget what you set on a payment ("Keep FreeAgent's")."""
        if not isinstance(body.get("url"), str):
            raise HTTPException(400, "url is required")
        service.reset_payment_settings(body["url"])
        return {"ok": True}

    @app.post("/api/statement/update-explanation")
    def statement_update_explanation(body: dict[str, Any]) -> dict[str, Any]:
        """Send the category / VAT / re-bill you changed to FreeAgent's own explanation."""
        if not isinstance(body.get("url"), str):
            raise HTTPException(400, "url is required")
        return {"queued": service.update_payment_explanation(body["url"])}

    @app.post("/api/statement/approve")
    def statement_approve(body: dict[str, Any]) -> dict[str, Any]:
        """Explain and approve a payment in FreeAgent, with its receipt if given."""
        if not isinstance(body.get("url"), str):
            raise HTTPException(400, "url is required")
        receipt = body.get("receipt_id")
        if receipt is not None and not isinstance(receipt, int):
            raise HTTPException(400, "receipt_id must be a number")
        return {"queued": service.approve_payment(body["url"], receipt)}

    @app.post("/api/statement/remove-explanation")
    def statement_remove_explanation(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body.get("url"), str):
            raise HTTPException(400, "url is required")
        return {"queued": service.remove_payment_explanation(body["url"])}

    @app.post("/api/statement/unexplain")
    def statement_unexplain(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body.get("url"), str):
            raise HTTPException(400, "url is required")
        return {"queued": service.unexplain_payment(body["url"])}

    @app.post("/api/statement/no-receipt")
    def statement_no_receipt(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body.get("url"), str):
            raise HTTPException(400, "url is required")
        service.mark_no_receipt(body["url"], body.get("reason") or None)
        return {"ok": True}

    @app.post("/api/files/upload")
    async def files_upload(request: Request, name: str, paid_by: str = "business") -> dict[str, Any]:
        """A dropped file, as the raw request body (`?name=receipt.jpg&paid_by=business|personal`)."""
        if paid_by not in ("business", "personal"):
            raise HTTPException(400, "paid_by is business or personal")
        data = await request.body()
        try:
            path = service.upload_file(name, data, paid_by)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"added": Path(path).name}

    @app.post("/api/statement/use-suggestion")
    def statement_use_suggestion(body: dict[str, Any]) -> dict[str, Any]:
        """"Use that email" / "Use that receipt": queued; read `outcome`."""
        if not isinstance(body.get("url"), str):
            raise HTTPException(400, "url is required")
        return {"queued": service.use_suggestion(body["url"])}

    @app.post("/api/statement/find-emails")
    def statement_find_emails() -> dict[str, Any]:
        """Look in Gmail now for receipts for payments without one."""
        return {"queued": service.find_emails()}

    @app.post("/api/statement/add-file")
    def statement_add_file(body: dict[str, Any]) -> dict[str, Any]:
        """The Mac's file picker, then the file goes into the receipt inbox."""
        script = ('POSIX path of (choose file with prompt "Choose the receipt" '
                  'of type {"public.image", "com.adobe.pdf"})')
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            return {"added": None}                      # cancelled
        try:
            added = service.add_receipt_file(result.stdout.strip(), body.get("paid_by") or "business")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"added": added}

    @app.post("/api/receipts/{receipt_id}/unfile")
    def unfile_receipt(receipt_id: int) -> dict[str, Any]:
        return {"queued": service.unfile(receipt_id)}

    @app.post("/api/receipts/{receipt_id}/update-claim")
    def update_claim(receipt_id: int) -> dict[str, Any]:
        return {"queued": service.update_claim(receipt_id)}

    @app.post("/api/freeagent/sync")
    def freeagent_sync() -> dict[str, Any]:
        return {"queued": service.sync_freeagent()}

    @app.post("/api/notifications/test")
    def notifications_test() -> dict[str, Any]:
        if not service.test_notification():
            raise HTTPException(400, "Notifications are only available in the Mac app")
        return {"ok": True}

    @app.post("/api/notifications/open-settings")
    def notifications_settings() -> dict[str, Any]:
        """Open this app's page in System Settings → Notifications."""
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=com.receiptbridge.app"],
            check=False,
        )
        return {"ok": True}

    # ---- receipt folders ----------------------------------------------------

    @app.post("/api/updates/check")
    def updates_check() -> dict[str, Any]:
        return {"started": service.check_for_updates()}

    @app.post("/api/updates/install")
    def updates_install() -> dict[str, Any]:
        try:
            return {"queued": service.install_update()}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/updates/open")
    def updates_open() -> dict[str, Any]:
        if not service.open_update_page():
            raise HTTPException(400, "No release page to open. Check for updates first.")
        return {"ok": True}

    @app.post("/api/settings/folders")
    def set_folders(body: dict[str, Any]) -> dict[str, Any]:
        try:
            service.set_folders(inbox=body.get("inbox") or None, archive=body.get("archive") or None)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/settings/folders/reset")
    def reset_folder(body: dict[str, Any]) -> dict[str, Any]:
        try:
            service.reset_folder(str(body.get("which", "")))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/settings/folders/create-subfolders")
    def create_subfolders() -> dict[str, Any]:
        try:
            service.create_inbox_folders()
        except OSError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    # ---- first-run setup guide ---------------------------------------------

    @app.post("/api/setup/inbox")
    def setup_inbox(body: dict[str, Any]) -> dict[str, Any]:
        """Make the receipt inbox ("icloud", or a folder from the picker) with
        Bank/ and Expense/ inside, and use it."""
        location = body.get("location")
        if not isinstance(location, str) or not location:
            raise HTTPException(400, "location is icloud or a folder path")
        try:
            return {"path": str(service.setup_inbox(location))}
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/setup/done")
    def setup_done(body: dict[str, Any]) -> dict[str, Any]:
        service.set_setup_done(body.get("done") is not False)
        return {"ok": True}

    @app.get("/api/setup/shortcut-qr")
    def shortcut_qr() -> Response:
        from .setup_guide import qr_png

        url = service.config.shortcut_url
        if not url:
            raise HTTPException(404, "No Shortcut link set")
        try:
            return Response(qr_png(url), media_type="image/png")
        except RuntimeError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/setup/open-shortcut")
    def open_shortcut() -> dict[str, Any]:
        """The Shortcut's link in the Mac's browser: Shortcuts on the Mac can
        add it too, and it syncs to the iPhone."""
        url = service.config.shortcut_url
        if not url.startswith("https://www.icloud.com/shortcuts/"):
            raise HTTPException(400, "No Shortcut link set")
        subprocess.run(["open", url], check=False)
        return {"ok": True}

    @app.post("/api/licence")
    async def licence(request: Request) -> dict[str, Any]:
        """A dropped licence file, as the raw request body."""
        from .licence import LicenceError

        data = await request.body()
        if len(data) > 64 * 1024:
            raise HTTPException(400, "That isn't a Receipt Bridge licence file.")
        try:
            return {"installed": service.install_licence(data)}
        except LicenceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/licence/choose")
    def licence_choose() -> dict[str, Any]:
        """The Mac's own file picker, for people who'd rather not drag."""
        from .licence import LicenceError

        script = 'POSIX path of (choose file with prompt "Choose your Receipt Bridge licence file" of type {"rbkey"})'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            return {"installed": None}                  # cancelled
        path = Path(result.stdout.strip())
        try:
            if path.stat().st_size > 64 * 1024:
                raise LicenceError("That isn't a Receipt Bridge licence file.")
            return {"installed": service.install_licence(path.read_bytes())}
        except (LicenceError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/settings/choose-folder")
    def choose_folder(body: dict[str, Any]) -> dict[str, Any]:
        """The Mac's own folder picker. Waits for the person, so it's a
        request that blocks on purpose; it touches nothing on the network."""
        prompt = {"inbox": "Choose the folder to look in for receipts",
                  "archive": "Choose where read receipts are moved to",
                  "setup": "Choose where to keep the Receipt Inbox folder"}.get(body.get("which"), "Choose a folder")
        script = f'POSIX path of (choose folder with prompt "{prompt}")'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            return {"path": None}                       # cancelled
        return {"path": result.stdout.strip().rstrip("/") or "/"}

    @app.post("/api/settings/open-folder")
    def open_folder(body: dict[str, Any]) -> dict[str, Any]:
        path = service.photo_inbox if body.get("which") == "inbox" else service.photo_archive
        if not path.is_dir():
            raise HTTPException(400, "That folder doesn't exist yet")
        subprocess.run(["open", str(path)], check=False)
        return {"ok": True}

    @app.post("/api/settings")
    def settings(body: Settings) -> dict[str, Any]:
        if body.auto_scan is not None:
            service.set_auto_scan(body.auto_scan)
        if body.waiting_days is not None:
            try:
                service.set_waiting_days(body.waiting_days)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if body.open_at_login is not None:
            try:
                service.set_open_at_login(body.open_at_login)
            except RuntimeError as exc:
                raise HTTPException(400, str(exc)) from exc
        if body.archive_delete_days is not None:
            try:
                service.set_archive_delete_days(body.archive_delete_days)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if (body.notify_enabled is not None or body.notify_frequency is not None
                or body.notify_categories is not None):
            try:
                service.set_notification_prefs(body.notify_frequency, body.notify_categories,
                                               body.notify_enabled)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if body.theme is not None:
            try:
                service.set_theme(body.theme)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    # ---- adding a supplier ---------------------------------------------
    #
    # These talk to Gmail, which is the point: they run only when the user
    # is actively building a rule, never while rendering a screen.

    samples: dict[str, Any] = {}

    def client_for(account: str):
        chosen = service.accounts.get(account) if account else None
        chosen = chosen or next(iter(service.accounts.list()), None)
        if chosen is None:
            raise HTTPException(400, "Connect a Gmail account first")
        return chosen, chosen.client(service.config.credentials_file, service.config.scopes)

    def sample(message_id: str, account: str):
        key = f"{account}:{message_id}"
        if key not in samples:
            _, client = client_for(account)
            if len(samples) > 20:
                samples.clear()
            samples[key] = client.fetch(message_id)
        return samples[key]

    @app.post("/api/suppliers/search")
    def supplier_search(body: SupplierSearch) -> list[dict[str, Any]]:
        q = body.q.strip()
        if len(q) < 2:
            return []
        results = []
        for account in service.accounts.list():
            client = account.client(service.config.credentials_file, service.config.scopes)
            try:
                stubs = client.search(f"{q} newer_than:2y", max_results=15)
                for row in client.headers([s["id"] for s in stubs]):
                    results.append({**row, "account": account.email})
            except Exception as exc:
                raise HTTPException(502, f"Gmail search failed: {exc}") from exc
        return results

    @app.post("/api/suppliers/analyse")
    def supplier_analyse(body: SupplierSample) -> dict[str, Any]:
        from dataclasses import asdict

        from . import supplier_builder

        message = sample(body.message_id, body.account)
        # The sender's other recent subjects show which words are fixed and
        # which change (order numbers, product names, months).
        siblings: list[str] = []
        try:
            _, client = client_for(body.account)
            from email.utils import parseaddr

            domain = supplier_builder._registrable(parseaddr(message.sender)[1].split("@")[-1])
            stubs = client.search(f"from:({domain}) newer_than:1y", max_results=25)
            siblings = [h["subject"] for h in client.headers([s["id"] for s in stubs])]
        except Exception:
            pass
        return asdict(supplier_builder.analyse(message, siblings))

    @app.post("/api/suppliers/preview")
    def supplier_preview(body: SupplierSample) -> dict[str, Any]:
        from . import supplier_builder
        from .gmail_client import with_date_window

        try:
            spec = supplier_builder.build_spec(body.choices)
        except ValueError as exc:
            return {"ok": False, "problem": str(exc)}
        result = supplier_builder.preview(spec, sample(body.message_id, body.account))
        if result.get("ok"):
            _, client = client_for(body.account)
            try:
                hits = client.search(with_date_window(spec["gmail_query"], None, 365), max_results=200)
                result["matches"] = len(hits)
            except Exception:
                result["matches"] = None
        return result

    @app.post("/api/suppliers")
    def supplier_create(body: SupplierSample) -> dict[str, Any]:
        from . import supplier_builder

        try:
            spec = supplier_builder.build_spec(body.choices)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        check = supplier_builder.preview(spec, sample(body.message_id, body.account))
        if not check.get("ok"):
            raise HTTPException(400, check.get("problem", "The rule doesn't work on the example"))
        path = supplier_builder.save(spec, service.config.watchers_dir)
        if body.choices.get("vat_treatment") == "reverse_charge":
            # where review keeps it, so it can be changed there too
            service.db.set_state(f"vat_treatment_for:{service._supplier_key(spec['vendor'])}",
                                 "reverse_charge")
        service.touch()
        # Its receipts now, not at the next six-hourly scan, which left a
        # new supplier at "0 receipts" while its editor said "matches 3".
        service.rescan_supplier(spec["id"])
        return {"id": spec["id"], "name": spec["name"], "file": path.name}

    @app.get("/api/suppliers/{watcher_id}")
    def supplier_get(watcher_id: str) -> dict[str, Any]:
        from . import supplier_editor

        path = service.supplier_path(watcher_id)
        if path is None:
            raise HTTPException(404, "No such supplier")
        rule = supplier_editor.read(path)
        return {**rule, "vat_treatment": supplier_vat_treatment(rule["name"])}

    def supplier_vat_treatment(name: str) -> str:
        """As printed or reverse charge: kept per supplier, where review keeps it."""
        key = service._supplier_key(name)
        return (service.db.get_state(f"vat_treatment_for:{key}") if key else None) or "printed"

    @app.post("/api/suppliers/{watcher_id}/check")
    def supplier_check(watcher_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Try edited settings against the inbox, without saving them."""
        from . import supplier_editor
        from .gmail_client import with_date_window
        from .watchers import Watcher, build_filename

        path = service.supplier_path(watcher_id)
        if path is None:
            raise HTTPException(404, "No such supplier")
        try:
            _, plain = supplier_editor.apply(path, body)
            watcher = Watcher.from_dict(plain, path)
        except Exception as exc:
            return {"ok": False, "problem": str(exc)}

        _, client = client_for("")
        try:
            hits = client.search(with_date_window(watcher.gmail_query, None, 365), max_results=200)
        except Exception as exc:
            return {"ok": False, "problem": f"Gmail search failed: {exc}"}
        result: dict[str, Any] = {"ok": True, "matches": len(hits), "latest": None}
        if hits:
            message = client.fetch(hits[0]["id"])
            if not watcher.matches(message):
                result["latest_problem"] = "The newest email found doesn't pass the rule's checks."
            else:
                try:
                    values = watcher.extract(message)
                    result["latest"] = build_filename(watcher.filename, values)
                    result.update(latest_vat(watcher, message, values))
                except Exception as exc:
                    result["latest_problem"] = str(exc)
        return result

    def latest_vat(watcher: Any, message: Any, values: dict[str, Any]) -> dict[str, Any]:
        """The VAT a scan would read from this email, and the rate it files at."""
        from decimal import Decimal

        from .filer import vat_rate
        from .pipeline import read_vat_from_text

        if values.get("vat") in (None, "") and not any(f.name == "vat" for f in watcher.fields):
            read_vat_from_text(values, message)
        vat = values.get("vat")
        if not vat or not values.get("total"):
            return {"vat": None, "vat_rate": ""}
        rate, problem = vat_rate({"vat": vat, "extra_json": json.dumps({"vat_lines": values.get("vat_lines")})},
                                 Decimal(str(values["total"])), True)
        return {"vat": float(vat), "vat_rate": f"{float(rate):g}%" if rate else "", "vat_problem": problem or ""}

    @app.post("/api/suppliers/{watcher_id}/edit")
    def supplier_edit(watcher_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from . import supplier_editor

        path = service.supplier_path(watcher_id)
        if path is None:
            raise HTTPException(404, "No such supplier")
        before = supplier_editor.read(path)
        treatment = body.pop("vat_treatment", None)
        if treatment not in (None, "printed", "reverse_charge"):
            raise HTTPException(400, "VAT is as printed or reverse charge")
        try:
            if body:
                supplier_editor.save(path, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        after = supplier_editor.read(path)
        if treatment:
            key = service._supplier_key(after["name"])
            if key:
                service.db.set_state(f"vat_treatment_for:{key}", treatment)
        if after["paid_with"] != before["paid_with"]:
            service.apply_paid_with(after["id"], after["paid_with"])
        service.touch()
        if any(after[k] != before[k] for k in ("domain", "subject", "mentions")):
            service.rescan_supplier(after["id"])     # emails it didn't match before
        return {**after, "vat_treatment": supplier_vat_treatment(after["name"])}

    @app.post("/api/suppliers/{watcher_id}/delete")
    def supplier_delete(watcher_id: str) -> dict[str, Any]:
        from . import supplier_editor

        path = service.supplier_path(watcher_id)
        if path is None:
            raise HTTPException(404, "No such supplier")
        name = supplier_editor.read(path)["name"]
        stored = supplier_editor.delete(path, service.config.data_dir / "deleted-suppliers")
        service.touch()
        return {"name": name, "undo": stored}

    @app.post("/api/suppliers/restore")
    def supplier_restore(body: dict[str, str]) -> dict[str, Any]:
        from . import supplier_editor

        try:
            path = supplier_editor.restore(
                body.get("undo", ""),
                service.config.data_dir / "deleted-suppliers",
                service.config.watchers_dir,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        service.touch()
        return supplier_editor.read(path)

    return app


__all__ = ["create_app", "PENDING", "EXPORTED", "IGNORED", "FAILED"]
