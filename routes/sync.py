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
        if len(_inspector_log) > 200:
            del _inspector_log[:-200]
    fh = _inspector_log_file[0]
    if fh:
        try:
            fh.write(msg + "\n\n")
            fh.flush()
        except Exception:
            pass


def _inspector_thread(stock_name: str, start_url: str):
    """Відкриває браузер, перехоплює мережу, пише в _inspector_log і на диск."""
    import time as _time
    from playwright.sync_api import sync_playwright as _spw
    _inspector_state["running"] = True
    _inspector_state["stock"]   = stock_name
    _inspector_log.clear()

    safe_name   = stock_name.replace(" ", "_")
    profile_dir = os.path.abspath(f"chrome_profile_{safe_name}")
    log_path    = os.path.join(INSPECTOR_LOG_DIR, f"{safe_name}.log")

    fh = open(log_path, "a", encoding="utf-8")
    _inspector_log_file[0] = fh
    import datetime as _dt
    fh.write(f"\n{'='*60}\n{_dt.datetime.now().isoformat()} — {stock_name}\n{'='*60}\n\n")
    fh.flush()

    _inspector_log_push(f"🔍 Opening {stock_name} — log: inspector_logs/{safe_name}.log")

    def _on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            url    = resp.url
            status = resp.status
            req    = resp.request
            try:
                body = resp.text().strip()
            except Exception:
                body = "<binary or unreadable>"
            preview = body[:30000] + ("…[truncated]" if len(body) > 30000 else "")
            hdrs = dict(req.headers)
            keep = ["authorization", "cookie", "x-api-key", "x-auth-token",
                    "x-requested-with", "x-end-app-name", "content-type", "accept"]
            hdrs_filtered = {k: v for k, v in hdrs.items() if k.lower() in keep}
            lines = [
                f"━━━ {req.method} {status} ━━━",
                f"URL: {url}",
            ]
            if hdrs_filtered:
                lines.append("HEADERS: " + json.dumps(hdrs_filtered, ensure_ascii=False))
            lines.append("BODY: " + preview)
            _inspector_log_push("\n".join(lines))
        except Exception as e:
            _inspector_log_push(f"[response hook error] {e}")

    try:
        with _spw() as _p:
            browser = _p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                no_viewport=True,
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
            if not browser.pages:
                pg = browser.new_page()
                try: pg.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                except Exception: pass

            browser.on("page", _attach_page)

            _inspector_log_push("✅ Browser open — log in and navigate to the earnings/sales page")
            while _inspector_state["running"]:
                try:
                    pages = browser.pages
                    if not pages:
                        pg = browser.new_page()
                        try: pg.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                        except Exception: pass
                    else:
                        pages[0].wait_for_timeout(500)
                except Exception:
                    break
            try: browser.close()
            except Exception: pass
    except Exception as ex:
        _inspector_log_push(f"🛑 Inspector error: {ex}")
    finally:
        _inspector_state["running"] = False
        _inspector_browser_ref[0]   = None
        _inspector_log_push("🔴 Inspector closed")
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
    stock = data.get("stock", "Depositphotos")
    url   = (_INSPECTOR_URLS.get(stock)
             or STOCK_URLS.get(stock)
             or "https://depositphotos.com/account/sales-history.html")
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
