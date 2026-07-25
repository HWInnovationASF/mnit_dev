import json
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = BASE_DIR / "remoteiot_worker.py"

# Device list is refreshed in the background every REFRESH_INTERVAL seconds so
# that GET /remoteiot/devices can respond instantly from cache instead of
# waiting on a live browser round-trip on every page load.
REFRESH_INTERVAL = 15
_cache = {"devices": None, "updated_at": None, "error": None}
_refresh_thread_started = False


class RemoteIoTError(Exception):
    pass


def _run_worker(*args, timeout=60):
    # Playwright's sync API refuses to run inside a thread/process that has an
    # asyncio event loop attached, which is effectively always true here once
    # eventlet monkey-patches threading in production (Render). A genuine
    # subprocess - not a thread, not multiprocessing's "spawn" (which
    # re-imports and re-executes this app's __main__, undoing the isolation) -
    # is the only way to guarantee a clean, un-monkey-patched interpreter.
    try:
        proc = subprocess.run(
            [sys.executable, str(WORKER_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RemoteIoTError("RemoteIoT worker timed out")

    output = (proc.stdout or "").strip()
    if not output:
        raise RemoteIoTError((proc.stderr or "worker produced no output").strip())

    try:
        result = json.loads(output.splitlines()[-1])
    except json.JSONDecodeError:
        raise RemoteIoTError((proc.stderr or output).strip())

    if not result.get("ok"):
        raise RemoteIoTError(result.get("error", "unknown RemoteIoT worker error"))

    return result


def _refresh_cache_once():
    try:
        result = _run_worker("list_devices")
        _cache["devices"] = result["devices"]
        _cache["updated_at"] = time.time()
        _cache["error"] = None
    except Exception as exc:
        _cache["error"] = str(exc)


def _background_refresh_loop():
    while True:
        time.sleep(REFRESH_INTERVAL)
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


def create_http_connection(serial):
    result = _run_worker("create_connection", serial)
    url = result["url"]

    # reflect the new connection immediately in the cache instead of waiting
    # for the next background refresh cycle
    if _cache["devices"]:
        for device in _cache["devices"]:
            if device["serial"] == serial:
                device["connection_url"] = url

    return url
