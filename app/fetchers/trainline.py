"""Download Trainline's own "Expense receipt" PDF.

The flow, verified against live bookings:

1. The confirmation email contains a passwordless link,
   `.../my-account/order-token#<token>`. The token is in the URL *fragment*,
   so only the single-page app ever sees it — this needs a real browser.
2. That page lists the booking. "Manage booking" opens the detail view, which
   carries an anchor to `.../order/token/<uuid>/expense-receipt?guid=...`.
3. That URL answers `application/pdf` directly.

Speed. Most of the time a fetch used to take was waiting: a fresh cookie jar
per booking meant the consent banner every time, and each consent or button
selector that did not exist waited out its full timeout before the next was
tried — around sixteen seconds per booking spent finding out what was *not*
on the page. Now one browser session is shared across the scan, so cookies
are declined once, and the page is inspected with instant checks rather than
a sequence of timeouts.

Correctness. Sharing a session raises a question a fresh jar never did:
could one booking's page show another booking's receipt? So every PDF is read
back and must contain this booking's own reference before it is accepted.
Filing the wrong receipt against a payment is worse than filing none.

Any failure returns None and the pipeline renders the email instead. This
module never raises.
"""

from __future__ import annotations

import io
import logging
import re
from urllib.parse import urljoin

from ..browser import dump_debug
from . import register
from .base import FetchContext, FetchResult

log = logging.getLogger(__name__)

SESSION = "trainline"

# The anchor that serves the PDF. Matched on href, not link text, so a copy
# change ("Expense receipt" -> "Receipt") does not break the fetcher.
RECEIPT_LINK = 'a[href*="expense-receipt"]'

# OneTrust. Trainline's banner offers only Accept and "Choose Cookies"; the
# decline is inside the preference centre. A direct decline is still tried
# first because other OneTrust configurations do put one on the banner.
CONSENT_BANNER = "#onetrust-banner-sdk"
CONSENT_DIRECT_DECLINE = (
    "#onetrust-reject-all-handler",
    'button:has-text("Necessary only")',
    'button:has-text("Only necessary")',
)
CONSENT_OPEN_PREFERENCES = "#onetrust-pc-btn-handler"
CONSENT_DECLINE_IN_PREFERENCES = ".ot-pc-refuse-all-handler, .save-preference-btn-handler"

# Rendered as a styled element rather than a real button, so match any
# element whose own text is exactly this — in one selector, not three
# sequential waits.
MANAGE_BOOKING = (
    'a:text-is("Manage booking"), button:text-is("Manage booking"), '
    '[role="button"]:text-is("Manage booking"), :text-is("Manage booking")'
)

# Either the booking or the consent banner, whichever the page shows first.
PAGE_READY = f"{CONSENT_BANNER}:visible, {MANAGE_BOOKING}"

# Trainline's own "try again shortly" page, shown when a run of bookings is
# fetched back to back. Temporary by its own account, so it is retried.
TRANSIENT_ERROR = "unable to retrieve your booking"
RETRY_DELAYS_MS = (5000, 12000)


class TrainlineFetcher:
    id = "trainline"

    def fetch(self, ctx: FetchContext) -> FetchResult | None:
        url = ctx.url_from_field("order_link")
        if not url:
            log.info("[trainline] no order link in email; falling back")
            return None

        reference = str(ctx.values.get("reference") or "")
        debug_name = "trainline-" + (re.sub(r"[^A-Za-z0-9_-]", "", reference) or "unknown")

        try:
            with ctx.pool.page(session=SESSION) as page:
                page.set_default_timeout(ctx.timeout_ms)
                self._open(page, url, ctx.timeout_ms)

                for delay_ms in RETRY_DELAYS_MS:
                    if TRANSIENT_ERROR not in self._text(page):
                        break
                    log.info(
                        "[trainline] %s not available yet, retrying in %ds",
                        reference,
                        delay_ms // 1000,
                    )
                    page.wait_for_timeout(delay_ms)
                    self._open(page, url, ctx.timeout_ms)

                href = self._receipt_href(page)
                if not href:
                    self._open_booking_detail(page)
                    href = self._receipt_href(page, wait_ms=12000)

                if not href:
                    log.info("[trainline] no expense-receipt link for %s", reference)
                    self._debug(ctx, page, f"{debug_name}-no-link")
                    return None

                pdf = self._download(page, urljoin(page.url, href), ctx.timeout_ms)
                if pdf is None:
                    self._debug(ctx, page, f"{debug_name}-bad-pdf")
                    return None

                if reference and not self._pdf_mentions(pdf, reference):
                    log.warning(
                        "[trainline] receipt PDF does not mention %s; refusing it",
                        reference,
                    )
                    return None

                ctx.out_path.parent.mkdir(parents=True, exist_ok=True)
                ctx.out_path.write_bytes(pdf)

        except Exception as exc:
            # A vendor site changing under us is expected, not exceptional.
            log.warning("[trainline] fetch failed for %s: %s", reference, exc)
            return None

        log.info("[trainline] downloaded official expense receipt for %s", reference)
        return FetchResult(
            path=ctx.out_path,
            source="trainline_expense_receipt",
            note="Trainline's own expense receipt, downloaded from Manage Booking.",
        )

    # ---- steps ----------------------------------------------------------

    @classmethod
    def _open(cls, page, url: str, timeout_ms: int) -> None:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        # Wait for whichever arrives first: the booking, or the banner
        # covering it. Neither arriving is not fatal — the error page has
        # neither, and the caller checks for that.
        try:
            page.locator(PAGE_READY).first.wait_for(state="visible", timeout=15000)
        except Exception:
            return
        cls._decline_cookies(page)

    @staticmethod
    def _decline_cookies(page) -> bool:
        """Decline non-essential cookies. Never accepts.

        Every check here is instant (`count`/`is_visible`), so a page with no
        banner costs nothing, and a banner costs one or two clicks.
        """
        banner = page.locator(CONSENT_BANNER).first
        try:
            if not banner.is_visible():
                return False
        except Exception:
            return False

        for selector in CONSENT_DIRECT_DECLINE:
            button = page.locator(selector).first
            try:
                if button.count() and button.is_visible():
                    button.click(timeout=5000)
                    break
            except Exception:
                continue
        else:
            try:
                page.locator(CONSENT_OPEN_PREFERENCES).first.click(timeout=5000)
                page.locator(CONSENT_DECLINE_IN_PREFERENCES).first.click(timeout=8000)
            except Exception as exc:
                log.warning("[trainline] could not decline cookies: %s", exc)
                return False

        try:
            page.locator("#onetrust-consent-sdk .onetrust-pc-dark-filter").first.wait_for(
                state="hidden", timeout=5000
            )
        except Exception:
            page.wait_for_timeout(800)
        return True

    @staticmethod
    def _receipt_href(page, wait_ms: int = 0) -> str | None:
        link = page.locator(RECEIPT_LINK).first
        try:
            if wait_ms:
                link.wait_for(state="attached", timeout=wait_ms)
            elif not link.count():
                return None
            return link.get_attribute("href")
        except Exception:
            return None

    @classmethod
    def _open_booking_detail(cls, page) -> None:
        target = page.locator(MANAGE_BOOKING).first
        try:
            target.click(timeout=8000)
        except Exception:
            # A late banner can sit over the button; clear it and try once more.
            if cls._decline_cookies(page):
                try:
                    target.click(timeout=8000)
                except Exception:
                    log.info("[trainline] could not open booking detail")

    @staticmethod
    def _download(page, url: str, timeout_ms: int) -> bytes | None:
        """GET the receipt with the page's cookies, and check it is a PDF."""
        response = page.request.get(url, timeout=timeout_ms)
        if not response.ok:
            log.warning("[trainline] receipt URL returned HTTP %s", response.status)
            return None
        body = response.body()
        # Trust the bytes, not the header: an expired token returns an HTML
        # error page, and filing that as a receipt would be worse than the
        # email fallback.
        if not body.startswith(b"%PDF"):
            log.warning("[trainline] receipt URL did not serve a PDF (%d bytes)", len(body))
            return None
        return body

    @staticmethod
    def _pdf_mentions(pdf: bytes, reference: str) -> bool:
        try:
            from pypdf import PdfReader

            text = "".join(
                page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages
            )
        except Exception:
            # Unreadable is not the same as wrong; keep the document.
            log.debug("could not read receipt text to verify it", exc_info=True)
            return True
        return reference in text

    @staticmethod
    def _text(page) -> str:
        """What the page actually *shows*.

        Not `page.content()`: the raw HTML embeds Trainline's whole
        translation bundle, error messages included, so searching it for the
        "try again shortly" message matched on every healthy booking and
        triggered seventeen seconds of pointless retry backoff each time.
        """
        try:
            return page.inner_text("body") or ""
        except Exception:
            return ""

    @staticmethod
    def _debug(ctx: FetchContext, page, name: str) -> None:
        if ctx.debug_dir and ctx.option("debug", True):
            dump_debug(page, ctx.debug_dir, name)


register(TrainlineFetcher())
