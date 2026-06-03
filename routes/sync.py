"""
routes/sync.py — Sync control and inspector endpoints.

Moved verbatim from main.py. No logic changes.
Routes: /api/sync/*, /api/inspector/*, /api/debug/log
"""

import json
import os
import sqlite3
import sys
import threading

from flask import Blueprint, Response, jsonify, request

from app_globals import DB_NAME, _BASE_DIR
from collectors.browser import _apply_stealth
from config import STOCK_URLS
from orchestrator import _collect_one_stock_global, _sync_all_global
from sync_state import (
    _session_new_keys, _session_new_keys_lock,
    _sync_log, _sync_log_lock, _sync_state, _sync_stop_flag, _sync_all_active,
    _LOG_FILE, get_headless, set_headless,
)

sync_bp = Blueprint('sync', __name__)

# ── Inspector state ───────────────────────────────────────────────────────────

_inspector_state: dict = {"running": False, "stock": ""}
_inspector_log: list   = []
_inspector_lock  = threading.Lock()
_inspector_browser_ref: list = [None]
_inspector_log_file = [None]

INSPECTOR_LOG_DIR = os.path.join(_BASE_DIR, "inspector_logs")
os.makedirs(INSPECTOR_LOG_DIR, exist_ok=True)

_INSPECTOR_URLS = {
    "Adobe Stock":   "https://contributor.stock.adobe.com/en/insights/sales-earnings",
    "Shutterstock":  "https://submit.shutterstock.com/earnings",
    "Getty Images":  "https://esp.gettyimages.com/contribute/stats",
    "iStock":        "https://esp.gettyimages.com/contribute/stats",
    "Depositphotos": "https://depositphotos.com/account/sales-history.html",
    "Pond5":         "https://www.pond5.com/dashboard/index/sales",
}


def _inspector_log_push(msg: str):
    with _inspector_lock:
        _inspector_log.append(msg)
        # keep a long in-memory tail (disk log is unbounded). Universal inspector
        # tracks whole login+browse sessions → don't truncate aggressively.
        if len(_inspector_log) > 5000:
            del _inspector_log[:-5000]
    fh = _inspector_log_file[0]
    if fh:
        try:
            fh.write(msg + "\n\n")
            fh.flush()
        except Exception:
            pass


_NOISE_HOSTS = ("datadoghq", "google-analytics", "googletagmanager", "doubleclick",
                "sentry.io", "/segment", "hotjar", "newrelic", "bam.nr-data",
                "amplitude", "mixpanel", "facebook.com/tr", "/rum", "clarity.ms",
                "browser-intake", "/collect?", "cdn.cookielaw")
_DATA_HINTS = ("earning", "sale", "download", "amount", "commission", "revenue",
               "payout", "statement", "thumbnail", "thumb", "item_id", "asset",
               "media", "price", "balance", "total_count")


def _looks_like_data(body: str) -> bool:
    s = body.lstrip()
    if not s or s[0] not in ("{", "["):
        return False
    return any(h in s[:3000].lower() for h in _DATA_HINTS)


def _build_curl(req) -> str:
    parts = [f"curl '{req.url}'", f"-X {req.method}"]
    for k, v in req.headers.items():
        if k.lower() in ("content-length", "host"):
            continue
        parts.append("-H '%s: %s'" % (k, v.replace("'", "'\\''")))
    try:
        pd = req.post_data
    except Exception:
        pd = None
    if pd:
        parts.append("--data-raw '%s'" % pd.replace("'", "'\\''"))
    return " ".join(parts)


def _enable_password_manager(profile_dir):
    """Ensure Chromium's password manager is on so logins can be saved + autofilled
    next time (easy re-login for the universal inspector)."""
    prefs_path = os.path.join(profile_dir, "Default", "Preferences")
    try:
        os.makedirs(os.path.dirname(prefs_path), exist_ok=True)
        prefs = {}
        if os.path.exists(prefs_path):
            with open(prefs_path, encoding="utf-8") as f:
                prefs = json.load(f)
        prefs["credentials_enable_service"] = True
        prefs.setdefault("profile", {})["password_manager_enabled"] = True
        with open(prefs_path, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    except Exception:
        pass


def _inspector_thread(stock_name: str, start_url: str):
    """Headed browser + network capture for reverse-engineering a stock API.

    Universal mode (stock_name == 'Universal'): a persistent `inspector_profile`
    (cookies + saved passwords persist) opens a blank page — the user types ANY
    URL in Chrome's address bar, logs in, and browses. Everything is captured:
      • XHR/fetch request body + response body
      • ⭐ highlight for responses that look like sales/earnings JSON
      • a copy-paste cURL line per request (replay the endpoint outside the browser)
      • a full HAR archive on disk
      • a cookie dump on close
    Tracking/analytics noise (datadog, GA, sentry…) is filtered out.
    """
    import datetime as _dt
    from playwright.sync_api import sync_playwright as _spw
    _inspector_state["running"] = True
    _inspector_state["stock"]   = stock_name
    _inspector_log.clear()

    safe_name = stock_name.replace(" ", "_")
    if stock_name == "Universal":
        profile_dir = os.path.join(_BASE_DIR, "inspector_profile")
        _enable_password_manager(profile_dir)
    else:
        profile_dir = os.path.join(_BASE_DIR, f"chrome_profile_{safe_name}")
    ts       = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(INSPECTOR_LOG_DIR, f"{safe_name}.log")
    har_path = os.path.join(INSPECTOR_LOG_DIR, f"{safe_name}_{ts}.har")

    fh = open(log_path, "a", encoding="utf-8")
    _inspector_log_file[0] = fh
    fh.write(f"\n{'='*60}\n{_dt.datetime.now().isoformat()} — {stock_name}\n{'='*60}\n\n")
    fh.flush()
    _inspector_log_push(f"🔍 {stock_name} — log: inspector_logs/{safe_name}.log | HAR: {os.path.basename(har_path)}")

    def _on_response(resp):
        try:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            url = resp.url
            if any(n in url for n in _NOISE_HOSTS):
                return
            try:
                body = resp.text()
            except Exception:
                body = "<binary or unreadable>"
            star    = "⭐ " if _looks_like_data(body) else ""
            preview = body[:30000] + ("…[truncated]" if len(body) > 30000 else "")
            try:
                post = req.post_data
            except Exception:
                post = None
            lines = [f"━━━ {star}{req.method} {resp.status} ━━━", f"URL: {url}"]
            if post:
                lines.append("REQUEST BODY: " + post[:5000])
            lines.append("RESPONSE: " + preview)
            lines.append("cURL: " + _build_curl(req))
            _inspector_log_push("\n".join(lines))
        except Exception as e:
            _inspector_log_push(f"[response hook error] {e}")

    browser = None
    try:
        with _spw() as _p:
            browser = _p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                no_viewport=True,
                record_har_path=har_path,
                record_har_content="embed",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"),
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-session-crashed-bubble",
                ],
            )
            _apply_stealth(browser)
            _inspector_browser_ref[0] = browser

            def _attach_page(page):
                page.on("response", _on_response)
            for p in browser.pages:
                _attach_page(p)
            browser.on("page", _attach_page)
            if not browser.pages:
                pg = browser.new_page()
                try: pg.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                except Exception: pass
            elif start_url and start_url != "about:blank":
                try: browser.pages[0].goto(start_url, wait_until="domcontentloaded", timeout=20000)
                except Exception: pass

            _inspector_log_push("✅ Browser open — type any URL in the address bar, log in, browse. ⭐ = likely sales/earnings JSON.")
            while _inspector_state["running"]:
                try:
                    pages = browser.pages
                    if not pages:
                        browser.new_page()
                    else:
                        pages[0].wait_for_timeout(500)
                except Exception:
                    break

            # dump cookies (for collector dev) before closing
            try:
                cookies = browser.cookies()
                cpath = os.path.join(INSPECTOR_LOG_DIR, f"{safe_name}_cookies.json")
                with open(cpath, "w", encoding="utf-8") as cf:
                    json.dump(cookies, cf, ensure_ascii=False, indent=1)
                _inspector_log_push(f"🍪 {len(cookies)} cookies → inspector_logs/{safe_name}_cookies.json")
            except Exception:
                pass
            try: browser.close()   # flushes the HAR
            except Exception: pass
    except Exception as ex:
        _inspector_log_push(f"🛑 Inspector error: {ex}")
    finally:
        _inspector_state["running"] = False
        _inspector_browser_ref[0]   = None
        _inspector_log_push(f"🔴 Inspector closed — HAR: inspector_logs/{os.path.basename(har_path)}")
        fh = _inspector_log_file[0]
        _inspector_log_file[0] = None
        if fh:
            try: fh.close()
            except Exception: pass


# ── Routes ────────────────────────────────────────────────────────────────────

@sync_bp.route('/api/sync/status', methods=['GET'])
def api_sync_status():
    """Поточний стан синхронізації."""
    return jsonify(_sync_state)


@sync_bp.route('/api/debug/log', methods=['GET'])
def api_debug_log():
    """Last N lines of app.log + sync log + platform info."""
    n = int(request.args.get('n', 300))
    out = []
    out.append(f"=== Platform: {sys.platform} ===")
    try:
        import platform as _pl
        out.append(f"OS: {_pl.platform()}")
        out.append(f"Python: {sys.version.split()[0]} at {sys.executable}")
    except Exception: pass
    out.append(f"App version: see ui-tauri/src-tauri/tauri.conf.json")
    out.append("")
    out.append("=== Recent sync log ===")
    with _sync_log_lock:
        out.extend(list(_sync_state.get('log', []))[-100:])
    out.append("")
    out.append(f"=== Last {n} lines of app.log ===")
    try:
        if os.path.exists(_LOG_FILE):
            with open(_LOG_FILE, errors='replace') as f:
                lines = f.readlines()
            out.extend(l.rstrip() for l in lines[-n:])
    except Exception as e:
        out.append(f"<failed to read app.log: {e}>")
    return ('\n'.join(out), 200, {'Content-Type': 'text/plain; charset=utf-8'})


@sync_bp.route('/api/sync/recent-keys', methods=['GET'])
def api_sync_recent_keys():
    """Keys of sales inserted during the most recent sync session."""
    with _session_new_keys_lock:
        return jsonify(list(_session_new_keys))


@sync_bp.route('/api/sync/recent-items', methods=['GET'])
def api_sync_recent_items():
    """Return full sale records for all keys in _session_new_keys."""
    with _session_new_keys_lock:
        keys = list(_session_new_keys)
    if not keys:
        return jsonify([])
    filters = []
    for k in keys:
        parts = k.split('|')
        if len(parts) == 4:
            filters.append((parts[0], parts[1][:10], parts[2], parts[3]))
    if not filters:
        return jsonify([])
    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        result = []
        for asset_id, date_prefix, stock, price_str in filters:
            try:
                price = float(price_str)
            except ValueError:
                continue
            row = conn.execute(
                "SELECT id, asset_id, price, thumb_url, date, stock FROM sales "
                "WHERE asset_id=? AND stock=? AND ABS(price-?)<=0.005 AND date LIKE ?",
                (asset_id, stock, price, date_prefix + '%')
            ).fetchone()
            if row:
                result.append({
                    'id': row[0], 'asset_id': row[1], 'price': row[2],
                    'thumb_url': row[3], 'date': (row[4] or '')[:10], 'stock': row[5]
                })
    result.sort(key=lambda x: x['date'], reverse=True)
    return jsonify(result)


@sync_bp.route('/api/sync/stream')
def api_sync_stream():
    """SSE stream для live логів синхронізації."""
    import time as _time
    def _generate():
        last = 0
        try:
            while True:
                with _sync_log_lock:
                    logs    = list(_sync_state.get("log", []))
                    running = _sync_state.get("running", False)
                if len(logs) > last:
                    for line in logs[last:]:
                        yield f"data: {json.dumps({'msg': line})}\n\n"
                    last = len(logs)
                if not running and last >= len(logs):
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    break
                _time.sleep(0.3)
        except GeneratorExit:
            pass
    return Response(_generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@sync_bp.route('/api/sync/start', methods=['POST'])
def api_sync_start():
    """Запустити синхронізацію — одного стоку або всіх."""
    if _sync_all_active[0]:
        return jsonify({"ok": False, "msg": "already running"})
    data  = request.get_json(force=True, silent=True) or {}
    stock = data.get('stock', '').strip()

    def _run_single(name, u):
        _sync_all_active[0]    = True
        _sync_stop_flag[0]     = False
        _sync_state["running"] = True
        _sync_state["log"]     = []
        try:
            _sync_log(f"🚀 Single-stock sync: {name}")
            _collect_one_stock_global(name, u, allow_login=True)
            _sync_log(f"✅ {name}: done")
        except Exception as ex:
            _sync_log(f"🛑 {name}: {ex}")
        finally:
            _sync_all_active[0]    = False
            _sync_stop_flag[0]     = False
            _sync_state["running"] = False

    if stock and stock in STOCK_URLS:
        url = STOCK_URLS[stock]
        threading.Thread(target=_run_single, args=(stock, url), daemon=True).start()
    else:
        threading.Thread(target=_sync_all_global, daemon=True).start()
    return jsonify({"ok": True})


@sync_bp.route('/api/sync/stop', methods=['POST'])
def api_sync_stop():
    """Зупинити поточну синхронізацію."""
    _sync_stop_flag[0] = True
    return jsonify({"ok": True})


@sync_bp.route('/api/sync/headless', methods=['POST'])
def api_sync_headless():
    """Перемикач headless режиму (true/false)."""
    data = request.get_json(force=True, silent=True) or {}
    set_headless(bool(data.get('headless', True)))
    return jsonify({"headless": get_headless()})


@sync_bp.route('/api/inspector/start', methods=['POST'])
def api_inspector_start():
    if _inspector_state["running"]:
        return jsonify({"ok": False, "msg": "already running"})
    data  = request.get_json(force=True, silent=True) or {}
    stock = data.get("stock") or "Universal"
    # Universal: blank page, user types the URL in Chrome. Per-stock deep-links
    # (legacy) still work if a known stock name is passed.
    url   = (data.get("url")
             or _INSPECTOR_URLS.get(stock)
             or STOCK_URLS.get(stock)
             or "about:blank")
    threading.Thread(target=_inspector_thread, args=(stock, url), daemon=True).start()
    return jsonify({"ok": True})


@sync_bp.route('/api/inspector/stop', methods=['POST'])
def api_inspector_stop():
    _inspector_state["running"] = False
    return jsonify({"ok": True})


@sync_bp.route('/api/inspector/stream')
def api_inspector_stream():
    import time as _time
    last = [0]
    def _gen():
        while True:
            with _inspector_lock:
                msgs    = _inspector_log[last[0]:]
                running = _inspector_state["running"]
            for msg in msgs:
                yield f"data: {json.dumps({'msg': msg})}\n\n"
            last[0] += len(msgs)
            if not running and not msgs:
                yield f"data: {json.dumps({'done': True})}\n\n"
                break
            _time.sleep(0.3)
    return Response(_gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@sync_bp.route('/api/inspector/status')
def api_inspector_status():
    return jsonify(_inspector_state)
