"""Receipt Bridge as a native macOS application.

A real NSWindow containing WKWebView (macOS's own web engine), showing the UI
served by the in-process API. Rules this file follows, each learned the hard
way:

1. **The window is never destroyed.** The previous version let AppKit free
   the window on close while Python still held it; a timer then touched the
   freed object and the process died with SIGTRAP in `object_getClass`.
   Closing now only hides the window (`windowShouldClose_` returns False), so
   no reference can ever outlive its object.

2. **Every Cocoa callback is guarded.** An exception escaping a PyObjC
   callback can abort the whole process; each one routes through `_safely`.

3. **No Cocoa calls from background threads.** Slow work lives in
   `ReceiptService` on its own threads. The shell only reads the service's
   snapshot, on the main thread, from one timer.

4. **PyObjC turns every method into an ObjC selector**, so private helpers
   carry `@objc.python_method`, and the delegate is held at module level
   because NSApplication only keeps a weak reference to it.

Closing the window keeps the app running in the Dock (like Mail), so
automatic checks continue; the Dock icon carries the to-file count.
"""

from __future__ import annotations

import logging
import subprocess
import threading

import objc
from AppKit import (
    NSAlert,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSColor,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSScreen,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWorkspace,
)
from Foundation import NSMakeRect, NSMakeSize, NSTimer, NSURL, NSURLRequest
from PyObjCTools import AppHelper
from UserNotifications import (
    UNAuthorizationOptionAlert,
    UNAuthorizationOptionSound,
    UNAuthorizationStatusAuthorized,
    UNAuthorizationStatusDenied,
    UNAuthorizationStatusNotDetermined,
    UNAuthorizationStatusProvisional,
    UNMutableNotificationContent,
    UNNotificationPresentationOptionBanner,
    UNNotificationPresentationOptionList,
    UNNotificationPresentationOptionSound,
    UNNotificationRequest,
    UNUserNotificationCenter,
)
from WebKit import WKWebView, WKWebViewConfiguration

from . import login_item
from .config import Config, load_config

log = logging.getLogger(__name__)

APP_NAME = "Receipt Bridge"
LOADING_HTML = """<!doctype html><meta name="color-scheme" content="light dark">
<style>html,body{height:100%;margin:0;font:13px -apple-system,sans-serif;color:#86868b;
display:flex;align-items:center;justify-content:center;background:Canvas}</style>
<body>Starting Receipt Bridge…</body>"""

_DELEGATE = None

class Notifier(NSObject, protocols=[objc.protocolNamed("UNUserNotificationCenterDelegate")]):
    """Notifications posted by Receipt Bridge itself.

    They used to go through `osascript`, so macOS credited them to Script
    Editor — and opened Script Editor in the Dock to do it. That was the
    price of avoiding a permission prompt while the process was "Python";
    now the app is a real executable with its own bundle id, the proper
    framework works, at the cost of asking once.
    """

    STATUS = {
        UNAuthorizationStatusAuthorized: "allowed",
        UNAuthorizationStatusProvisional: "allowed",
        UNAuthorizationStatusDenied: "denied",
        UNAuthorizationStatusNotDetermined: "not_asked",
    }

    def initWithOpener_(self, opener):
        self = objc.super(Notifier, self).init()
        if self is None:
            return None
        self.opener = opener
        self.status = "unknown"
        self.checked_at = 0.0
        self.center = UNUserNotificationCenter.currentNotificationCenter()
        self.center.setDelegate_(self)
        return self

    @objc.python_method
    def ask(self):
        """Ask once; macOS remembers the answer and never shows this again."""

        def done(granted, error):
            self.status = "allowed" if granted else "denied"
            log.info(
                "notification permission: granted=%s error=%s",
                bool(granted),
                f"{error.domain()} {error.code()}: {error.localizedDescription()}" if error is not None else None,
            )

        self.center.requestAuthorizationWithOptions_completionHandler_(
            UNAuthorizationOptionAlert | UNAuthorizationOptionSound, done
        )

    @objc.python_method
    def refresh(self, max_age: float = 10.0):
        """Re-read the permission — the user may change it in System Settings."""
        import time

        if time.monotonic() - self.checked_at < max_age:
            return
        self.checked_at = time.monotonic()

        def got(settings):
            status = self.STATUS.get(settings.authorizationStatus(), "unknown")
            if status != self.status:
                log.info("notification status: %s (raw %s)", status, settings.authorizationStatus())
            self.status = status

        AppHelper.callAfter(self.center.getNotificationSettingsWithCompletionHandler_, got)

    @objc.python_method
    def send(self, title: str, body: str):
        """Safe from any thread: the work is handed to the main thread."""
        AppHelper.callAfter(self._post, title, body)

    @objc.python_method
    def _post(self, title: str, body: str):
        import uuid

        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(
            str(uuid.uuid4()), content, None
        )

        def added(error):
            if error is not None:
                log.warning("notification not shown: %s", error.localizedDescription())
            else:
                log.info("notification posted: %s", body)

        self.center.addNotificationRequest_withCompletionHandler_(request, added)

    # Show banners even while the app is in front: otherwise "Test" in
    # Settings, pressed with the app frontmost, would appear to do nothing.
    def userNotificationCenter_willPresentNotification_withCompletionHandler_(
        self, center, notification, handler
    ):
        handler(
            UNNotificationPresentationOptionBanner
            | UNNotificationPresentationOptionList
            | UNNotificationPresentationOptionSound
        )

    # Clicking a notification opens the window — even if it had been closed.
    def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        self, center, response, handler
    ):
        try:
            AppHelper.callAfter(self.opener)
        finally:
            handler()


THEME_APPEARANCES = {"dark": "NSAppearanceNameDarkAqua", "light": "NSAppearanceNameAqua"}


def _apply_theme(theme: str) -> None:
    """Make the whole app — title bar, menus, the web view — follow a theme.

    None means "follow the system". The page's CSS follows too, because
    WKWebView reports the app's appearance as prefers-color-scheme.
    """
    from AppKit import NSAppearance

    name = THEME_APPEARANCES.get(theme)
    NSApplication.sharedApplication().setAppearance_(
        NSAppearance.appearanceNamed_(name) if name else None
    )


class ScriptBridge(NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
    """Receives window.webkit.messageHandlers.rb.postMessage(...) from the page."""

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            body = message.body()
            theme = body.get("theme") if hasattr(body, "get") else None
            if theme is not None:
                _apply_theme(str(theme))
        except Exception:
            log.exception("script message failed")


class AppDelegate(NSObject):
    def initWithConfig_(self, config):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.config = config
        self.service = None
        self.api_url = None
        self.window = None
        self.webview = None
        self.login_item = None
        self.last_badge = None
        return self

    # ---- safety ---------------------------------------------------------

    @objc.python_method
    def _safely(self, label, fn):
        try:
            return fn()
        except Exception:
            log.exception("%s failed", label)
            return None

    # ---- lifecycle ------------------------------------------------------

    def applicationDidFinishLaunching_(self, notification):
        self._safely("launch", self._launch)

    @objc.python_method
    def _launch(self):
        from .service import ReceiptService

        self.notifier = Notifier.alloc().initWithOpener_(self._show)
        self.service = ReceiptService(self.config, notifier=self.notifier)
        self.service.restart = self._restart
        # Before the window exists, so it never flashes the wrong theme.
        _apply_theme(self.service.theme)

        self._build_menus()
        self._build_window()
        self._start_server()
        self.service.start()
        # After the window is up, so the one-time prompt has context.
        self.notifier.ask()

        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.15, self, "checkServer:", None, True
        )
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            3.0, self, "refreshBadge:", None, True
        )
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    @objc.python_method
    def _restart(self):
        """Quit and open again, to run a version just installed. Safe from
        any thread. Only when running as the app: there's nothing to reopen
        otherwise."""
        import os

        from .updates import relaunch_after_exit

        bundle = login_item.running_bundle()
        if bundle is None:
            log.info("updated; not running as an app, so not restarting")
            return False
        log.info("restarting %s", bundle)
        relaunch_after_exit(os.getpid(), bundle)
        AppHelper.callAfter(NSApplication.sharedApplication().terminate_, None)
        return True

    def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
        return False

    def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, flag):
        self._safely("reopen", self._show)
        return True

    def applicationWillTerminate_(self, notification):
        if self.service is not None:
            self._safely("stop service", self.service.stop)

    # ---- window ---------------------------------------------------------

    @objc.python_method
    def _build_window(self):
        width, height = 1240, 820
        screen = NSScreen.mainScreen()
        if screen is not None:
            frame = screen.visibleFrame()
            width = min(width, int(frame.size.width) - 60)
            height = min(height, int(frame.size.height) - 60)

        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height), style, NSBackingStoreBuffered, False
        )
        # Belt and braces with windowShouldClose_: AppKit must never free this.
        window.setReleasedWhenClosed_(False)
        window.setTitle_(APP_NAME)
        window.setMinSize_(NSMakeSize(900, 560))
        window.setBackgroundColor_(NSColor.windowBackgroundColor())
        window.setFrameAutosaveName_("ReceiptBridgeMain")
        window.setDelegate_(self)

        configuration = WKWebViewConfiguration.alloc().init()
        self.bridge = ScriptBridge.alloc().init()
        configuration.userContentController().addScriptMessageHandler_name_(self.bridge, "rb")
        webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, width, height), configuration
        )
        webview.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        webview.setNavigationDelegate_(self)
        webview.setUIDelegate_(self)
        try:
            # No white flash before the page paints in dark mode.
            webview.setValue_forKey_(False, "drawsBackground")
        except Exception:
            pass
        webview.loadHTMLString_baseURL_(LOADING_HTML, None)

        window.setContentView_(webview)
        if not window.setFrameUsingName_("ReceiptBridgeMain"):
            window.center()
        window.makeKeyAndOrderFront_(None)
        self.window = window
        self.webview = webview

    def windowShouldClose_(self, sender):
        # Hide rather than close. See rule 1 in the module docstring.
        self._safely("hide window", lambda: sender.orderOut_(None))
        return False

    @objc.python_method
    def _show(self):
        if self.window is not None:
            self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def showWindow_(self, sender):
        self._safely("show window", self._show)

    @objc.python_method
    def _start_server(self):
        import socket

        import uvicorn

        from .api import create_app

        host = self.config.web.get("host", "127.0.0.1")
        port = int(self.config.web.get("port", 8765))
        with socket.socket() as probe:
            if probe.connect_ex((host, port)) == 0:
                # Something else holds the port (a terminal `cli.py serve`,
                # usually). Take any free one rather than failing to start.
                port = 0
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        port = sock.getsockname()[1]

        server = uvicorn.Server(
            uvicorn.Config(create_app(self.service), log_level="warning")
        )
        server.install_signal_handlers = lambda: None
        threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, name="receipt-http", daemon=True
        ).start()
        self.server = server
        self.api_url = f"http://{host}:{port}/"
        log.info("serving on %s", self.api_url)

    def checkServer_(self, timer):
        def run():
            if getattr(self, "server", None) is not None and self.server.started:
                timer.invalidate()
                self.webview.loadRequest_(
                    NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.api_url))
                )

        self._safely("check server", run)

    def refreshBadge_(self, timer):
        def run():
            pending = self.service.snapshot()["counts"]["pending"] if self.service else 0
            label = str(pending) if pending else ""
            if label != self.last_badge:
                NSApplication.sharedApplication().dockTile().setBadgeLabel_(label)
                self.window.setTitle_(f"{APP_NAME} — {pending} to file" if pending else APP_NAME)
                self.last_badge = label
            if self.login_item is not None:
                self.login_item.setState_(1 if login_item.is_enabled() else 0)

        self._safely("refresh badge", run)

    # ---- web view -------------------------------------------------------

    def webView_didFailProvisionalNavigation_withError_(self, webview, navigation, error):
        log.warning("page failed to load: %s", error.localizedDescription())

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
        self, webview, configuration, action, features
    ):
        # target="_blank" and window.open: nothing in the app needs a second
        # window, so any such link is external and belongs in the browser.
        # Only web and mail links: the Emails view shows senders' own links,
        # and NSWorkspace would open a file: URL to an app by running it.
        url = action.request().URL()
        scheme = str(url.scheme() or "").lower() if url is not None else ""
        if scheme in ("http", "https", "mailto"):
            self._safely("open link", lambda: NSWorkspace.sharedWorkspace().openURL_(url))
        return None

    # WKWebView shows no dialog for confirm() / alert() unless asked to, and
    # confirm() then answers "cancel" silently: Unfile, Disconnect and Sign out
    # did nothing in the app. These show the Mac's own dialog instead.

    def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(
        self, webview, message, frame, handler
    ):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Cancel")
        handler(alert.runModal() == 1000)          # NSAlertFirstButtonReturn

    def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(
        self, webview, message, frame, handler
    ):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.runModal()
        handler()

    @objc.python_method
    def _js(self, script):
        if self.webview is not None:
            self.webview.evaluateJavaScript_completionHandler_(script, None)

    # ---- menus ----------------------------------------------------------

    @objc.python_method
    def _item(self, title, action, key="", target=None, modifiers=None):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if target is not None:
            item.setTarget_(target)
        if modifiers is not None:
            item.setKeyEquivalentModifierMask_(modifiers)
        return item

    @objc.python_method
    def _submenu(self, main, title, items):
        menu = NSMenu.alloc().initWithTitle_(title)
        for item in items:
            menu.addItem_(item if item is not None else NSMenuItem.separatorItem())
        holder = NSMenuItem.alloc().init()
        holder.setSubmenu_(menu)
        main.addItem_(holder)
        return menu

    @objc.python_method
    def _build_menus(self):
        """Standard menu bar. Without an Edit menu, Cmd+C/V do nothing at all."""
        main = NSMenu.alloc().init()
        self.login_item = self._item("Open at Login", "toggleLogin:", "", self)
        self.login_item.setState_(1 if login_item.is_enabled() else 0)

        self._submenu(main, APP_NAME, [
            self._item(f"About {APP_NAME}", "showAbout:", "", self),
            None,
            self._item("Settings…", "showSettings:", ",", self),
            self.login_item,
            None,
            self._item(f"Hide {APP_NAME}", "hide:", "h"),
            self._item("Hide Others", "hideOtherApplications:", "h", None, 1 << 19 | 1 << 20),
            None,
            self._item(f"Quit {APP_NAME}", "terminate:", "q"),
        ])
        self._submenu(main, "File", [
            self._item("Check for Receipts", "checkNow:", "r", self),
            None,
            self._item("Open Exports Folder", "openExports:", "", self),
            None,
            self._item("Close Window", "performClose:", "w"),
        ])
        self._submenu(main, "Edit", [
            self._item("Undo", "undo:", "z"),
            self._item("Redo", "redo:", "Z"),
            None,
            self._item("Cut", "cut:", "x"),
            self._item("Copy", "copy:", "c"),
            self._item("Paste", "paste:", "v"),
            self._item("Select All", "selectAll:", "a"),
        ])
        self._submenu(main, "View", [
            self._item("To File", "showPending:", "1", self),
            self._item("Filed", "showFiled:", "2", self),
            self._item("Ignored", "showIgnored:", "3", self),
            None,
            self._item("Reload", "reloadPage:", "R", self),
        ])
        window_menu = self._submenu(main, "Window", [
            self._item("Minimise", "performMiniaturize:", "m"),
            self._item("Zoom", "performZoom:", ""),
            None,
            self._item(APP_NAME, "showWindow:", "0", self),
        ])
        app = NSApplication.sharedApplication()
        app.setMainMenu_(main)
        app.setWindowsMenu_(window_menu)

    # Menu actions. Each is a one-liner into the service or the page.

    def checkNow_(self, sender):
        self._safely("check now", lambda: self.service and self.service.scan())

    def showAbout_(self, sender):
        # Settings → About: version, licence, EULA, open-source software
        self._safely("about", lambda: (self._show(), self._js("window.rbShowAbout && rbShowAbout()")))

    def showSettings_(self, sender):
        self._safely("settings", lambda: (self._show(), self._js("window.rbShowSettings && rbShowSettings()")))

    def showPending_(self, sender):
        self._safely("view", lambda: (self._show(), self._js("rbShowView('pending')")))

    def showFiled_(self, sender):
        self._safely("view", lambda: (self._show(), self._js("rbShowView('exported')")))

    def showIgnored_(self, sender):
        self._safely("view", lambda: (self._show(), self._js("rbShowView('ignored')")))

    def reloadPage_(self, sender):
        self._safely("reload", lambda: self.webview.reload_(None))

    def openExports_(self, sender):
        def run():
            self.config.export_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["open", str(self.config.export_dir)], check=False)

        self._safely("open exports", run)

    def toggleLogin_(self, sender):
        self._safely("toggle login item", lambda: self._toggle_login(sender))

    @objc.python_method
    def _toggle_login(self, sender):
        try:
            login_item.set_enabled(not login_item.is_enabled())
        except RuntimeError as exc:
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Open at Login needs the installed app")
            alert.setInformativeText_(str(exc))
            alert.runModal()
        sender.setState_(1 if login_item.is_enabled() else 0)


_INSTANCE_LOCK = None


def _claim_single_instance(config: Config) -> bool:
    """True if this is the only running copy.

    macOS normally refuses a second launch of an app, but not once the
    bundle has been replaced on disk while it runs — which reinstalling
    does — because LaunchServices then sees two different registrations.
    That produced two engines with two schedulers sharing one database.
    An OS-held file lock catches every route, and the OS releases it if
    the process dies, so a crash can't lock the app out.
    """
    import fcntl

    global _INSTANCE_LOCK
    config.data_dir.mkdir(parents=True, exist_ok=True)
    handle = (config.data_dir / "app.lock").open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _INSTANCE_LOCK = handle
    return True


def _bring_existing_forward() -> None:
    import os

    from AppKit import NSRunningApplication

    for app in NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        "com.receiptbridge.app"
    ):
        if app.processIdentifier() != os.getpid():
            app.activateWithOptions_(0)
            return


def main(config: Config | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("googleapiclient.discovery_cache", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    config = config or load_config()
    if not _claim_single_instance(config):
        log.info("already running; bringing that window forward instead")
        _bring_existing_forward()
        return

    global _DELEGATE
    application = NSApplication.sharedApplication()
    application.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    _DELEGATE = AppDelegate.alloc().initWithConfig_(config)
    application.setDelegate_(_DELEGATE)
    application.run()


if __name__ == "__main__":
    main()
