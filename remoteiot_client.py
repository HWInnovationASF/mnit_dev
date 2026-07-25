import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv, set_key, unset_key

BASE_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = BASE_DIR / "remoteiot_worker.py"
SITES_FILE = BASE_DIR / "remoteiot_sites.json"
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)

# Device list per site is refreshed in the background every REFRESH_INTERVAL
# seconds so GET /remoteiot/devices can respond instantly from cache instead
# of waiting on a live browser round-trip on every page load.
REFRESH_INTERVAL = 15
_cache_lock = threading.Lock()
_cache = {}  # site_key -> {"devices": [...], "updated_at": ts, "error": str|None}
_refresh_thread_started = False


class RemoteIoTError(Exception):
    pass


def _session_file(site_key):
    return BASE_DIR / f".remoteiot_session_{site_key}.json"


def load_sites():
    if not SITES_FILE.exists():
        return []
    try:
        with open(SITES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_sites(sites):
    with open(SITES_FILE, "w", encoding="utf-8") as f:
        json.dump(sites, f, ensure_ascii=False, indent=4)


def get_site(site_key):
    for site in load_sites():
        if site["key"] == site_key:
            return site
    return None


def _password_env_key(site_key):
    return f"REMOTEIOT_PASSWORD_{site_key.upper()}"


def get_site_password(site_key):
    return os.getenv(_password_env_key(site_key))


def add_or_update_site(key, label, email, password):
    sites = load_sites()
    for site in sites:
        if site["key"] == key:
            site.update(label=label, email=email)
            break
    else:
        sites.append({"key": key, "label": label, "email": email})
    _save_sites(sites)

    # password is kept out of the JSON file (which gets committed to git) and
    # stored in .env instead, one REMOTEIOT_PASSWORD_<KEY> var per site
    env_key = _password_env_key(key)
    set_key(str(ENV_FILE), env_key, password)
    os.environ[env_key] = password


def delete_site(key):
    sites = [s for s in load_sites() if s["key"] != key]
    _save_sites(sites)
    unset_key(str(ENV_FILE), _password_env_key(key))
    os.environ.pop(_password_env_key(key), None)
    with _cache_lock:
        _cache.pop(key, None)


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
        detail = result.get("traceback") or result.get("error", "unknown RemoteIoT worker error")
        raise RemoteIoTError(detail)

    return result


def _worker_config(site):
    return json.dumps(
        {
            "email": site.get("email"),
            "password": get_site_password(site["key"]),
            "session_file": str(_session_file(site["key"])),
        }
    )


def _refresh_site_once(site):
    key = site["key"]
    entry = _cache.setdefault(key, {"devices": None, "updated_at": None, "error": None})
    try:
        result = _run_worker("list_devices", _worker_config(site))
        entry["devices"] = result["devices"]
        entry["updated_at"] = time.time()
        entry["error"] = None
    except Exception as exc:
        entry["error"] = str(exc)


def _background_refresh_loop():
    while True:
        for site in load_sites():
            _refresh_site_once(site)
        time.sleep(REFRESH_INTERVAL)


def _ensure_background_refresh():
    global _refresh_thread_started
    if _refresh_thread_started:
        return
    _refresh_thread_started = True
    threading.Thread(target=_background_refresh_loop, daemon=True).start()


# Start populating the cache as soon as this module loads (app startup),
# instead of waiting for the first incoming request. A cold browser-automation
# round trip takes ~60s on Render's free tier - long enough to blow past
# Render/Cloudflare's proxy timeout if it happens inside a request. Callers
# of list_devices() just read whatever's cached so far (possibly still empty
# right after boot) instead of blocking on it.
_ensure_background_refresh()


def list_devices(site_key):
    entry = _cache.get(site_key)
    if entry is None:
        return []
    if entry["devices"] is None and entry["error"]:
        raise RemoteIoTError(entry["error"])
    return entry["devices"] or []


def cache_updated_at(site_key):
    entry = _cache.get(site_key)
    return entry["updated_at"] if entry else None


def create_http_connection(site_key, serial):
    site = get_site(site_key)
    if site is None:
        raise RemoteIoTError(f"unknown site: {site_key}")

    # more steps than list_devices (open menu, pick protocol, submit, confirm
    # dialog) and Render's free-tier CPU is slow - give it more headroom
    result = _run_worker("create_connection", _worker_config(site), serial, timeout=150)
    url = result["url"]

    # reflect the new connection immediately in the cache instead of waiting
    # for the next background refresh cycle
    entry = _cache.get(site_key)
    if entry and entry["devices"]:
        for device in entry["devices"]:
            if device["serial"] == serial:
                device["connection_url"] = url

    return url
