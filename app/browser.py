"""Shared Chromium instance, used for both PDF rendering and receipt fetching.

Launching Chromium costs a second or two, so one browser is reused for a whole
scan rather than started per email. Playwright is imported lazily so the rest
of the app still runs if it was never installed.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

# Hosts that exist only to count opens. Blocking them means previewing a
# receipt in this app does not report back to the sender.
TRACKER_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.com/tr",
    "facebook.net",
    "scorecardresearch.com",
    "hotjar.com",
    "segment.io",
    "branch.io",
    "adjust.com",
    "appsflyer.com",
    "sendgrid.net/wf/open",
    "mktoresp.com",
    "list-manage.com/track",
    "clicks.trainline",
    "/open.aspx",
    "/wf/open",
    "utm.gif",
    "pixel.gif",
    "spacer.gif",
)


class BrowserUnavailable(RuntimeError):
    """Playwright or its Chromium build is not installed."""


class BrowserPool:
    """Lazily-launched Chromium, reused across a scan."""

    def __init__(self, headless: bool = True, block_trackers: bool = True):
        self.headless = headless
        self.block_trackers = block_trackers
        self._playwright: Any = None
        self._browser: Any = None
        # Named contexts kept alive for the whole scan. See `page(session=)`.
        self._sessions: dict[str, Any] = {}

    def __enter__(self) -> "BrowserPool":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "playwright is not installed — run: pip install playwright"
            ) from exc

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
        except Exception as exc:
            self.close()
            raise BrowserUnavailable(
                "could not launch Chromium — run: playwright install chromium "
                f"({exc})"
            ) from exc
        return self._browser

    @contextmanager
    def page(
        self, accept_downloads: bool = False, session: str | None = None
    ) -> Iterator[Any]:
        """A browser page.

        Without `session`, every call gets a fresh, isolated cookie jar.

        With `session="name"`, pages share one context for the life of the
        pool. That matters for sites with consent banners: decline cookies
        once and the choice sticks for every later booking in the same scan,
        instead of re-running the whole consent dance per receipt. For
        Trainline that dance was most of the time each fetch took.
        """
        if session:
            context = self._sessions.get(session)
            if context is None:
                context = self._new_context(accept_downloads=True)
                self._sessions[session] = context
            page = context.new_page()
            try:
                yield page
            finally:
                try:
                    page.close()
                except Exception:
                    log.debug("page already closed", exc_info=True)
            return

        context = self._new_context(accept_downloads=accept_downloads)
        page = context.new_page()
        try:
            yield page
        finally:
            try:
                context.close()
            except Exception:
                log.debug("browser context already closed", exc_info=True)

    def _new_context(self, accept_downloads: bool) -> Any:
        browser = self._ensure_browser()
        context = browser.new_context(
            accept_downloads=accept_downloads,
            viewport={"width": 1280, "height": 1600},
            locale="en-GB",
            timezone_id="Europe/London",
        )
        if self.block_trackers:
            context.route("**/*", self._maybe_block)
        return context

    @staticmethod
    def _maybe_block(route: Any, request: Any) -> None:
        url = request.url.lower()
        if any(host in url for host in TRACKER_HOSTS):
            route.abort()
        else:
            route.continue_()

    def close(self) -> None:
        for context in self._sessions.values():
            try:
                context.close()
            except Exception:
                log.debug("error closing session context", exc_info=True)
        self._sessions.clear()

        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                log.debug("error closing browser", exc_info=True)
            self._browser = None

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                log.debug("error stopping playwright", exc_info=True)
            self._playwright = None


def dump_debug(page: Any, debug_dir: Path, name: str) -> Path | None:
    """Save a screenshot and the DOM so a failed fetch can be diagnosed.

    Vendors change their markup without warning; when a fetcher stops working
    these two files are what turn "it fell back again" into a fixable selector.
    """
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        shot = debug_dir / f"{name}.png"
        page.screenshot(path=str(shot), full_page=True)
        (debug_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
        return shot
    except Exception:
        log.debug("could not write debug output", exc_info=True)
        return None
