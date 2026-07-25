import atexit
import multiprocessing
import os
import re
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

REMOTEIOT_EMAIL = os.getenv("REMOTEIOT_EMAIL")
REMOTEIOT_PASSWORD = os.getenv("REMOTEIOT_PASSWORD")
LOGIN_URL = "https://remoteiot.com/portal/?link=login"
PORTAL_URL = "https://remoteiot.com/portal/"

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / ".remoteiot_session.json"

# Playwright's sync API refuses to run inside a thread that has an asyncio
# event loop attached. On Render, flask-socketio/eventlet monkey-patches
# threading process-wide, so a plain worker thread isn't a real isolated OS
# thread anymore ("It looks like you are using Playwright Sync API inside the
# asyncio loop"). A genuine child process (spawned, not forked, so it doesn't
# inherit the parent's monkey-patched modules) sidesteps that entirely.
_mp_context = multiprocessing.get_context("spawn")
_executor = ProcessPoolExecutor(max_workers=1, mp_context=_mp_context)
_playwright = None
_browser = None

# Device list is refreshed in the background every REFRESH_INTERVAL seconds so
# that GET /remoteiot/devices can respond instantly from cache instead of
# waiting on a live browser round-trip on every page load.
REFRESH_INTERVAL = 15
_cache = {"devices": None, "updated_at": None, "error": None}
_refresh_thread_started = False


class RemoteIoTError(Exception):
    pass


def _get_browser():
    global _playwright, _browser
    if _browser is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(headless=True)
        atexit.register(_shutdown)
    return _browser


def _shutdown():
    global _playwright, _browser
    if _browser is not None:
        _browser.close()
        _browser = None
    if _playwright is not None:
        _playwright.stop()
        _playwright = None


def _is_logged_in(page):
    return page.query_selector(".v-menubar") is not None or "Devices" in page.url


def _login(page):
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    email_input = page.wait_for_selector("input", state="visible")
    email_input.click()
    email_input.type(REMOTEIOT_EMAIL, delay=5)
    page.get_by_text("Next", exact=True).click()

    pw_input = page.wait_for_selector("input[type=password]", state="visible")
    pw_input.click()
    pw_input.type(REMOTEIOT_PASSWORD, delay=5)
    page.get_by_text("Sign In", exact=True).click()
    page.wait_for_selector("table tr", state="attached", timeout=15000)

    if not _is_logged_in(page):
        raise RemoteIoTError("Login to RemoteIoT failed - check REMOTEIOT_EMAIL/REMOTEIOT_PASSWORD")


def _open_logged_in_page():
    if not REMOTEIOT_EMAIL or not REMOTEIOT_PASSWORD:
        raise RemoteIoTError("REMOTEIOT_EMAIL/REMOTEIOT_PASSWORD not configured")

    browser = _get_browser()

    if SESSION_FILE.exists():
        context = browser.new_context(
            storage_state=str(SESSION_FILE), viewport={"width": 1400, "height": 900}
        )
        page = context.new_page()
        page.goto(PORTAL_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("table tr, input", state="visible", timeout=8000)
        except Exception:
            pass
        if _is_logged_in(page):
            return context, page
        context.close()

    context = browser.new_context(viewport={"width": 1400, "height": 900})
    page = context.new_page()
    _login(page)
    context.storage_state(path=str(SESSION_FILE))
    return context, page


def _parse_devices(page):
    devices = []
    rows = page.query_selector_all("table tr")
    for row in rows:
        text = row.inner_text()
        if not text.strip():
            continue
        cells = [c.strip() for c in text.split("\n") if c.strip()]
        # icon-only cells (hamburger/monitor/swap buttons) render as FontAwesome
        # private-use-area glyphs with no real text - drop them before reading columns
        cells = [c for c in cells if not all(0xE000 <= ord(ch) <= 0xF8FF for ch in c)]
        if len(cells) < 3:
            continue
        serial_match = re.search(r"\b1[0-9a-f]{15}\b", text)
        if not serial_match:
            continue
        connection_match = re.search(r"proxy\d+\.remoteiot\.com:\d+", text)
        devices.append(
            {
                "name": cells[0],
                "serial": serial_match.group(0),
                "connection_url": (
                    f"http://{connection_match.group(0)}" if connection_match else None
                ),
                "raw": cells,
            }
        )
    return devices


def _list_devices_impl():
    context, page = _open_logged_in_page()
    try:
        return _parse_devices(page)
    finally:
        context.close()


def _refresh_cache_once():
    # runs in the parent process - _list_devices_impl executes in the
    # dedicated child process, its plain-data return value crosses back over
    # the ProcessPoolExecutor's IPC pipe so it's safe to store here
    try:
        devices = _executor.submit(_list_devices_impl).result()
        _cache["devices"] = devices
        _cache["updated_at"] = time.time()
        _cache["error"] = None
    except Exception as exc:
        _cache["error"] = str(exc)


def _background_refresh_loop():
    while True:
        time.sleep(REFRESH_INTERVAL)
        if REMOTEIOT_EMAIL and REMOTEIOT_PASSWORD:
            _refresh_cache_once()


def _ensure_background_refresh():
    global _refresh_thread_started
    if _refresh_thread_started:
        return
    _refresh_thread_started = True
    threading.Thread(target=_background_refresh_loop, daemon=True).start()


def list_devices():
    _ensure_background_refresh()

    if _cache["devices"] is None:
        # first call ever - nothing cached yet, block once to populate it
        _refresh_cache_once()

    if _cache["devices"] is None and _cache["error"]:
        raise RemoteIoTError(_cache["error"])

    return _cache["devices"] or []


def cache_updated_at():
    return _cache["updated_at"]


def _create_http_connection_impl(serial):
    context, page = _open_logged_in_page()
    try:
        target_row = None
        for row in page.query_selector_all("table tr"):
            if serial in (row.inner_text() or ""):
                target_row = row
                break

        if target_row is None:
            raise RemoteIoTError(f"Device with serial {serial} not found")

        target_row.query_selector(".v-menubar").click()
        page.get_by_text("Connect Port", exact=True).wait_for(state="visible")
        page.get_by_text("Connect Port", exact=True).click()

        combo = page.wait_for_selector(".v-window .v-filterselect", state="visible")
        combo.query_selector(".v-filterselect-button").click(force=True)
        page.get_by_text("HTTP", exact=True).wait_for(state="visible")
        page.get_by_text("HTTP", exact=True).click()

        page.get_by_text("Submit", exact=True).click()

        # if a tunnel already exists, a confirm dialog appears first - keep it instead of replacing
        try:
            no_button = page.get_by_text("NO", exact=True)
            no_button.wait_for(state="visible", timeout=5000)
            no_button.click()
        except PlaywrightTimeoutError:
            pass

        link = page.wait_for_selector("a:has-text('Open URL')", state="visible", timeout=15000)
        return link.get_attribute("href")
    finally:
        context.close()


def create_http_connection(serial):
    url = _executor.submit(_create_http_connection_impl, serial).result()

    # reflect the new connection immediately in the cache (parent process)
    # instead of waiting for the next background refresh cycle
    if _cache["devices"]:
        for device in _cache["devices"]:
            if device["serial"] == serial:
                device["connection_url"] = url

    return url
