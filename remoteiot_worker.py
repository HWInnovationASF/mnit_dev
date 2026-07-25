"""
Standalone Playwright worker for RemoteIoT automation.

Deliberately does NOT import app.py, Flask, SocketIO, or anything MQTT/eventlet
related. It's invoked as a plain subprocess (see remoteiot_client.py) so it
never inherits eventlet's monkey-patched threading, and - unlike
multiprocessing's "spawn" start method - never re-imports/re-executes app.py's
module-level setup either.
"""

import base64
import json
import re
import sys
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

LOGIN_URL = "https://remoteiot.com/portal/?link=login"
PORTAL_URL = "https://remoteiot.com/portal/"


class RemoteIoTError(Exception):
    pass


def _is_logged_in(page):
    return page.query_selector(".v-menubar") is not None or "Devices" in page.url


def _login(page, email, password):
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    email_input = page.wait_for_selector("input", state="visible")
    email_input.click()
    email_input.type(email, delay=5)
    page.get_by_text("Next", exact=True).click()

    pw_input = page.wait_for_selector("input[type=password]", state="visible")
    pw_input.click()
    pw_input.type(password, delay=5)
    page.get_by_text("Sign In", exact=True).click()

    try:
        page.wait_for_selector("table tr", state="attached", timeout=25000)
    except PlaywrightTimeoutError:
        screenshot_b64 = base64.b64encode(page.screenshot(full_page=True)).decode("ascii")
        raise RemoteIoTError(
            f"Login did not reach the Devices dashboard within timeout "
            f"(stuck on: {page.url}). SCREENSHOT_B64:{screenshot_b64}"
        )

    if not _is_logged_in(page):
        raise RemoteIoTError("Login to RemoteIoT failed - check the site's email/password")


def _open_logged_in_page(browser, email, password, session_file):
    if not email or not password:
        raise RemoteIoTError("site email/password not configured")

    if session_file.exists():
        context = browser.new_context(
            storage_state=str(session_file), viewport={"width": 1400, "height": 900}
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
    _login(page, email, password)
    context.storage_state(path=str(session_file))
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

        status_icon = row.query_selector("img")
        status_src = status_icon.get_attribute("src") if status_icon else ""
        online = "online.png" in (status_src or "")

        devices.append(
            {
                "name": cells[0],
                "serial": serial_match.group(0),
                "connection_url": (
                    f"http://{connection_match.group(0)}" if connection_match else None
                ),
                "online": online,
            }
        )
    return devices


def list_devices(browser, email, password, session_file):
    context, page = _open_logged_in_page(browser, email, password, session_file)
    try:
        return _parse_devices(page)
    finally:
        context.close()


def create_http_connection(browser, email, password, session_file, serial):
    context, page = _open_logged_in_page(browser, email, password, session_file)
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


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    config = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    email = config.get("email")
    password = config.get("password")
    session_file = Path(config.get("session_file", ".remoteiot_session.json"))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                if action == "list_devices":
                    result = {"ok": True, "devices": list_devices(browser, email, password, session_file)}
                elif action == "create_connection":
                    serial = sys.argv[3]
                    result = {
                        "ok": True,
                        "url": create_http_connection(browser, email, password, session_file, serial),
                    }
                else:
                    result = {"ok": False, "error": f"unknown action: {action!r}"}
            finally:
                browser.close()
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}

    print(json.dumps(result))


if __name__ == "__main__":
    main()
