"""
sync_state.py — Shared mutable sync state, logging, and record-saving helper.
NO Flask, NO Playwright dependencies. Safe to import from collectors/.

Moved here from main.py (logic unchanged — only location changed):
  - _sync_state, _sync_stop_flag, _sync_all_active
  - _session_new_keys + lock, _sync_log_lock
  - _sync_log, _app_log
  - _save_record
"""

import atexit
import os
import tempfile
import threading
from datetime import datetime

from db import is_already_saved, save_to_db

# ── App-level file logger ─────────────────────────────────────────────────────

_LOG_DIR  = os.environ.get("STOCK_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
try:
    os.makedirs(_LOG_DIR, exist_ok=True)
except Exception:
    pass
_LOG_FILE = os.path.join(_LOG_DIR, "app.log")
try:
    _log_file_handle = open(_LOG_FILE, "a", encoding="utf-8", errors="replace", buffering=1)
except Exception:
    _LOG_FILE = os.path.join(tempfile.gettempdir(), "stock_automation_app.log")
    _log_file_handle = open(_LOG_FILE, "a", encoding="utf-8", errors="replace", buffering=1)
atexit.register(lambda: _log_file_handle.close())


def _app_log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try: print(line, flush=True)
    except Exception: pass
    try: _log_file_handle.write(line + "\n")
    except Exception: pass


# ── Sync state ────────────────────────────────────────────────────────────────

# Глобальний стан синку для /api/sync/status
_sync_state: dict = {"running": False, "log": [], "progress": ""}
_sync_stop_flag: list  = [False]   # [0] = True → зупинити синк
_sync_all_active: list = [False]   # [0] = True → синк вже запущено
# Keys of sales inserted during the CURRENT/MOST-RECENT sync session.
# Cleared at the start of each sync, appended by _save_record per insert.
# Client reads via GET /api/sync/recent-keys to paint blue highlights —
# this is more reliable than client-side diffing of cached items.
_session_new_keys: list = []
_session_new_keys_lock = threading.Lock()
_sync_log_lock = threading.Lock()  # guards _sync_state["log"] reads/writes

# Current sync-batch number. All rows saved by ONE sync run share it, so the
# feed can keep sync batches in recency order while sorting by sale date WITHIN
# each batch. Bumped ONCE per sync start (_begin_sync_batch) — never per stock.
_sync_batch: list = [0]


def _begin_sync_batch():
    """Start a new sync batch: next number after the largest one in the DB.
    Call ONCE at the start of a sync run (Sync All or single-stock)."""
    import sqlite3
    from db import DB_NAME
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            r = c.execute("SELECT MAX(sync_batch) FROM sales").fetchone()
            _sync_batch[0] = int(r[0] or 0) + 1
    except Exception:
        _sync_batch[0] += 1
    return _sync_batch[0]

# Headless mode toggle — use a list so importers get a shared mutable reference.
# DO NOT use a plain bool — `from sync_state import _headless_mode` makes a copy.
_headless_flag: list = [True]   # _headless_flag[0] = current headless setting
_headless_lock = threading.Lock()


def get_headless() -> bool:
    with _headless_lock:
        return _headless_flag[0]


def set_headless(val: bool):
    with _headless_lock:
        _headless_flag[0] = val


def _sync_log(msg: str):
    with _sync_log_lock:
        _sync_state["log"].append(msg)
        _sync_state["log"] = _sync_state["log"][-200:]
        _sync_state["progress"] = msg


# ── Record saving ─────────────────────────────────────────────────────────────

def _save_record(d: dict):
    """In-process save (no HTTP). Used by collectors as the ONLY save path.

    ⚠️ DO NOT switch back to `_http_executor.submit(requests.post('/update', ...))`:
    sync thread completes before background POSTs reach Flask → SSE 'done' fires
    with stale DB → UI shows old data. The bug took 2 sessions to diagnose.

    Also appends the inserted key to _session_new_keys for backend-tracked
    blue-highlight diff (client reads via /api/sync/recent-keys). The dedup
    guard before insert prevents already-known sales from polluting that list."""
    if not d: return
    raw_date = str(d.get('date') or '').strip()
    # Parse the collector's date. Accepted: ISO-T (with/without offset or Z),
    # the app's own 19-char "YYYY-MM-DD HH:MM:SS", m/d/Y, d/m/Y, YYYY-MM-DD.
    # NEVER fall back to datetime.now(): an unparsed date silently stamped
    # "today" corrupts Analytics/period stats. Log loudly and drop instead.
    dt = None
    if raw_date:
        core = raw_date.split('+')[0].split('Z')[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                    "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(core, fmt)
                break
            except Exception: pass
    if dt is None:
        _sync_log(f"⚠️ [{d.get('stock','?')}] unparsable date {raw_date!r} for asset "
                  f"{d.get('asset_id','?')} — record DROPPED")
        return
    # Date-only input (no time component) must be deduped by day+price, not by
    # exact datetime: the normalized "00:00:00" would otherwise make every
    # second same-day sale of an asset look like an exact duplicate (bug that
    # dropped Dreamstime/Alamy/Depositphotos/SS same-day sales at other prices).
    has_time = 'T' in raw_date or (' ' in raw_date and len(raw_date) > 10)
    d['date']  = dt.strftime("%Y-%m-%d %H:%M:%S")
    d['stock'] = d.get('stock', 'Adobe Stock')
    dedup_date = d['date'] if has_time else d['date'][:10]
    # Dedup guard: skip if already in DB (collectors call this without checking).
    # Without this, re-syncs would balloon _session_new_keys with duplicates.
    try:
        if is_already_saved(d['stock'], d.get('asset_id', ''), d.get('price', 0), dedup_date):
            return
    except Exception as ex:
        _sync_log(f"⚠️ dedup check failed for {d.get('stock')}/{d.get('asset_id')}: {ex}")
    d.setdefault('sync_batch', _sync_batch[0])
    if not save_to_db(d):
        _sync_log(f"⚠️ [{d['stock']}] DB insert failed for asset {d.get('asset_id','?')} "
                  f"({d['date']}) — sale NOT saved")
        return
    # Record the key for client blue-highlight diff. Uses 10-char date prefix
    # so it matches /api/feed which slices date to YYYY-MM-DD.
    try:
        key = f"{d.get('asset_id','')}|{d['date'][:10]}|{d['stock']}|{float(d.get('price') or 0):.2f}"
        with _session_new_keys_lock:
            _session_new_keys.append(key)
    except Exception:
        pass
