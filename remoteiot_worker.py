"""
Standalone Playwright worker for RemoteIoT automation.

Deliberately does NOT import app.py, Flask, SocketIO, or anything MQTT/eventlet
related. It's invoked as a plain subprocess (see remoteiot_client.py) so it
never inherits eventlet's monkey-patched threading, and - unlike
multiprocessing's "spawn" start method - never re-imports/re-executes app.py's
module-level setup either.
"""

import json
import os
import re
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

REMOTEIOT_EMAIL = os.getenv("REMOTEIOT_EMAIL")
REMOTEIOT_PASSWORD = os.getenv("REMOTEIOT_PASSWORD")
LOGIN_URL = "https://remoteiot.com/portal/?link=login"
PORTAL_URL = "https://remoteiot.com/portal/"
SESSION_FILE = BASE_DIR / ".remoteiot_session.json"


class RemoteIoTError(Exception):
    pass


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


def _open_logged_in_page(browser):
    if not REMOTEIOT_EMAIL or not REMOTEIOT_PASSWORD:
        raise RemoteIoTError("REMOTEIOT_EMAIL/REMOTEIOT_PASSWORD not configured")

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
            }
        )
    return devices


def list_devices(browser):
    context, page = _open_logged_in_page(browser)
    try:
        return _parse_devices(page)
    finally:
        context.close()


def create_http_connection(browser, serial):
    context, page = _open_logged_in_page(browser)
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

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                if action == "list_devices":
                    result = {"ok": True, "devices": list_devices(browser)}
                elif action == "create_connection":
                    serial = sys.argv[2]
                    result = {"ok": True, "url": create_http_connection(browser, serial)}
                else:
                    result = {"ok": False, "error": f"unknown action: {action!r}"}
            finally:
                browser.close()
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}

    print(json.dumps(result))


if __name__ == "__main__":
    main()
