"""The hidden browser that prints emails as PDFs (app/browser.py).

An email is from anyone, so the page it's printed on runs nothing and loads
only what it needs to look like itself. The live tests print a hostile email
through Chromium itself, and are skipped where it isn't installed.
"""

from __future__ import annotations

import http.server
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.browser import BrowserPool, BrowserUnavailable, printable_request  # noqa: E402
from app.pdf import render_email_to_pdf  # noqa: E402


class Skip(Exception):
    pass


def test_an_email_may_load_its_pictures_styles_and_fonts():
    for url, kind in (("https://cdn.example.com/logo.png", "image"),
                      ("https://fonts.example.com/a.woff2", "font"),
                      ("http://example.com/mail.css", "stylesheet"),
                      ("data:image/png;base64,AAAA", "image")):
        assert printable_request(url, kind), (url, kind)


def test_an_email_may_load_nothing_else():
    for url, kind in (("https://example.com/x.js", "script"),
                      ("https://example.com/", "document"),         # a frame, or a redirect away
                      ("https://example.com/api", "fetch"),
                      ("https://example.com/api", "xhr"),
                      ("https://example.com/a.mp4", "media"),
                      ("file:///etc/passwd", "image"),
                      ("ftp://example.com/a.png", "image")):
        assert not printable_request(url, kind), (url, kind)


def test_an_email_never_loads_from_this_mac_or_its_network():
    for host in ("127.0.0.1:8765", "localhost:8765", "[::1]", "192.168.1.1", "10.0.0.5", "172.16.3.4",
                 "169.254.169.254", "0.0.0.0", "127.1", "2130706433", "0x7f.0.0.1", "[::ffff:127.0.0.1]",
                 "printer.local", "router.lan", "app.localhost"):
        assert not printable_request(f"http://{host}/pixel.png", "image"), host


@contextmanager
def _pool():
    with BrowserPool() as pool:
        try:
            pool._ensure_browser()
        except BrowserUnavailable as exc:
            raise Skip(str(exc)) from exc
        yield pool


@contextmanager
def _local_server():
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", hits
    finally:
        server.shutdown()
        server.server_close()


def test_a_printed_email_runs_no_script_and_reaches_nothing_on_this_mac():
    with _pool() as pool, _local_server() as (base, hits):
        hostile = (f'<p>Total £12.00</p><img src="x" onerror="document.title=\'ran\';fetch(\'{base}/from-js\')">'
                   f'<svg onload="document.title=\'ran\'"></svg><script src="{base}/a.js">'
                   f'<img src="{base}/pixel.png"><iframe src="{base}/frame"></iframe>'
                   f'<meta http-equiv="refresh" content="0;url={base}/refresh">')
        with pool.page(printing=True) as page:
            page.set_content(f"<!doctype html><html><body>{hostile}</body></html>", wait_until="load")
            page.wait_for_timeout(1000)
            assert page.title() == "", page.title()
            assert "Total £12.00" in page.inner_text("body")
        assert hits == [], hits


def test_an_email_still_prints_to_a_pdf():
    with _pool() as pool, tempfile.TemporaryDirectory() as tmp:
        out = render_email_to_pdf(pool, "<p>Thanks for your order</p><img src=x onerror=alert(1)>",
                                  {"vendor": "Shop", "total": 12.0, "currency": "GBP"}, Path(tmp) / "r.pdf")
        assert out.read_bytes().startswith(b"%PDF")


def test_other_pages_keep_javascript():
    """Supplier sites (Trainline's booking page) are apps: they need it."""
    with _pool() as pool, pool.page() as page:
        page.set_content("<script>document.title='ran'</script>")
        assert page.title() == "ran"


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
            print(f"  PASS  {name}")
        except Skip as exc:
            print(f"  SKIP  {name}: {exc}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print("\nAll tests passed." if not failures else f"\n{failures} test(s) failed.")
    sys.exit(1 if failures else 0)
