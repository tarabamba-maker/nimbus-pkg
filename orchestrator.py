"""
orchestrator.py — Sync orchestration: run collectors, coordinate parallel/sequential sync.

Moved here from main.py (logic unchanged — only location changed):
  - _run_collector_global   — opens Playwright browser + dispatches to stock collector
  - _collect_one_stock_global — direct API fast path → Playwright fallback per stock
  - _sync_all_global        — parallel sales sync then MS+, then rebuild-matches
"""

import json
import os
import threading
import shutil
import sqlite3
import time

from db import DB_NAME
from sync_state import (
    _sync_log, _sync_stop_flag, _sync_state, _sync_all_active,
    _session_new_keys, _session_new_keys_lock,
)
from collectors.browser import (
    _open_browser_context, _apply_stealth, _is_login_url, _do_login_flow_global,
)
from collectors.alamy import _alamy_collect

_BASE_DIR = os.environ.get("STOCK_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
RECIPES_DIR          = os.path.join(_BASE_DIR, "recipes")
MS_LIBRARY_FILE      = os.path.join(RECIPES_DIR, "ms_library.json")
PROCESSED_DATES_FILE = os.path.join(RECIPES_DIR, "_processed_dates.json")

from config import STOCK_URLS

# Stocks collected via the global native-login + direct-HTTPS mechanism (no
# Playwright). "stock name": "module:function" of its *_collect_direct(). Migrating
# a stock here = it stops opening any browser to collect. See _collect_one_stock_global.
# Contract (tri-state):
#   True         — collected successfully → month-gated stocks are marked synced;
#   "transient"  — network/API hiccup, nothing (or only part) collected → skip
#                  this sync, do NOT mark synced, retry next sync, no login window;
#   False        — the session is invalid/missing → caller opens a native Chrome
#                  login window and retries once.
# Returning True on a transient failure used to mark Getty/Freepik/Envato as
# "collected this cycle" and silently lose a whole month.
# Only Alamy stays off this list — Playwright is its PRIMARY transport (ASP.NET
# UpdatePanel pagination, see CLAUDE.md).
_DIRECT_COLLECTORS = {
    "Dreamstime": "collectors.dreamstime:_dreamstime_collect_direct",
    "123RF":      "collectors.rf123:_rf123_collect_direct",
    "PIXTA":      "collectors.pixta:_pixta_collect_direct",
    "Freepik":    "collectors.freepik:_freepik_collect_direct",
    "Depositphotos": "collectors.depositphotos:_depositphotos_collect_direct",
    "Envato":     "collectors.envato:_envato_collect_direct",
    "Adobe Stock":  "collectors.adobe:_adobe_collect_direct",
    "Shutterstock": "collectors.shutterstock:_shutterstock_api_collect_direct",
    "Getty Images": "collectors.getty:_getty_collect_direct",
    "Microstock+":  "collectors.ms_plus:_ms_plus_collect_direct",
}

# Login URLs for the native Chrome login window. Getty needs TWO tabs: the TSV
# export lives on accountmanagement.* while the `ccw` thumb token is minted only
# on esp.* — one SSO login covers both, but each tab must be visited/refreshed.
_DIRECT_LOGIN_URLS = {
    "Getty Images": ["https://accountmanagement.gettyimages.com/Reports/Export",
                     "https://esp.gettyimages.com/contribute/stats"],
}

# Month-granularity stocks: their source data only changes once a month, so sync
# them once per cycle. {stock: (min_day, sales-stock names in DB, state key)}.
# Getty statements publish ~21st; Freepik invoice validates by the 10th; Envato's
# previous month is settled in item_performance well before the 11th.
_MONTHLY_GATES = {
    "Getty Images": (21, ("iStock", "iStockphoto"), "Getty/iStock_last_sync"),
    "Freepik":      (11, ("Freepik",),              "Freepik_last_sync"),
    "Envato":       (11, ("Envato",),               "Envato_last_sync"),
}
# Days after a cycle opens during which the stock keeps re-syncing even if it
# already ran this cycle. Getty statement periods keep gaining rows for several
# days after the ~21st (measured 2026-06-23), so a single early run must not
# lock the cycle. 0 = classic once-per-cycle.
_MONTHLY_SETTLE_DAYS = {"Getty Images": 7}

# Only ONE native Chrome login window at a time. Sync All runs 4 collectors in
# parallel; without this a network outage that made several collectors report
# "needs login" opened up to 4 Chrome windows simultaneously.
_login_lock = threading.Lock()


def _load_processed_dates():
    try:
        with open(PROCESSED_DATES_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _monthly_gate_skip(name):
    """True → this month-granularity stock was already synced this cycle (or the
    cycle hasn't opened yet) — skip. Cold start (no rows for the stock) never
    skips. A cycle opens on the stock's min_day each month."""
    cfg = _MONTHLY_GATES.get(name)
    if not cfg:
        return False
    min_day, db_stocks, state_key = cfg
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            ph = ",".join("?" * len(db_stocks))
            has_data = c.execute(
                f"SELECT 1 FROM sales WHERE stock IN ({ph}) LIMIT 1", db_stocks
            ).fetchone() is not None
    except Exception:
        has_data = False
    if not has_data:
        return False
    from datetime import date as _date
    today = _date.today()
    pd = _load_processed_dates()
    last_str = pd.get(state_key) or (pd.get("Getty_last_run") if name == "Getty Images" else None)
    if not last_str:
        return False
    try:
        last = _date.fromisoformat(last_str)
    except Exception:
        return False
    # Most recent cycle opening on/before today:
    if today.day >= min_day:
        cycle_start = _date(today.year, today.month, min_day)
    else:
        py, pm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        cycle_start = _date(py, pm, min_day)
    settle = _MONTHLY_SETTLE_DAYS.get(name, 0)
    if settle and (today - cycle_start).days < settle:
        return False   # inside the settling window → always re-sync
    if last >= cycle_start:
        nm = cycle_start.month % 12 + 1
        ny = cycle_start.year + (1 if cycle_start.month == 12 else 0)
        nxt = _date(ny, nm, min_day)
        _sync_log(f"📅 [{name}] already collected this cycle — next sync from {nxt.strftime('%d.%m.%Y')}")
        return True
    return False


def _mark_monthly_synced(name):
    cfg = _MONTHLY_GATES.get(name)
    if not cfg:
        return
    from datetime import date as _date
    pd = _load_processed_dates()
    pd[cfg[2]] = _date.today().isoformat()
    try:
        os.makedirs(RECIPES_DIR, exist_ok=True)
        with open(PROCESSED_DATES_FILE, "w") as f:
            json.dump(pd, f, indent=2)
    except Exception:
        pass


def _dispatch_collector(stock_name, pw_page):
    """Run one stock's Playwright collector. Returns True / 'needs_login'.
    Only Alamy reaches this — every other stock collects via _DIRECT_COLLECTORS
    (native login + direct HTTPS) and never opens a browser."""
    if stock_name == "Alamy":
        return _alamy_collect(pw_page)
    _sync_log(f"[{stock_name}] ⚠️ no Playwright collector — stock should be in _DIRECT_COLLECTORS")
    return True


def _run_collector_global(p, profile_dir, stock_name, start_url, headless, allow_login=False):
    """Open a browser, run the stock's collector, and — UNIFORMLY for every stock —
    open a login window whenever the session is invalid/expired.

    Two triggers, one behaviour:
      1. landing on a login URL after navigating to start_url, or
      2. the collector returning 'needs_login' (auth / anti-bot failure).
    When allow_login (single-stock button) → open the login window and retry once.
    When not (Sync All) → log that the stock needs login and skip it (no popup)."""
    wait_cond = "networkidle" if stock_name == "Shutterstock" else "domcontentloaded"
    # Real system Chrome (not bundled Chromium): Shutterstock (DataDome) and
    # Dreamstime (aggressive WAF) — the genuine Chrome fingerprint passes cleaner.
    channel = "chrome" if stock_name in ("Shutterstock", "Dreamstime") else None
    _real = channel == "chrome"
    _px = stock_name == "Dreamstime"   # PerimeterX Press & Hold site
    settle = 5 if stock_name == "Shutterstock" else 3

    def _open():
        # Always reuse the SAME channel/stealth on (re)open — otherwise a post-login
        # reopen would drop channel='chrome' and re-trip the anti-bot (old bug).
        b = _open_browser_context(p, profile_dir, headless, channel=channel, px_mode=_px)
        _apply_stealth(b, real_chrome=_real, px_mode=_px)
        pg = b.pages[0] if b.pages else b.new_page()
        pg.goto(start_url, wait_until=wait_cond, timeout=60000)
        time.sleep(settle)
        return b, pg

    def _needs_login_now():
        """Uniform: not allow_login → log + signal skip; else open login window,
        wait for the user, and reopen the browser fresh."""
        nonlocal browser, pw_page
        if not allow_login:
            _sync_log(f"[{stock_name}] 🔒 потрібен логін — натисни кнопку «{stock_name}» щоб увійти")
            return False
        _sync_log(f"[{stock_name}] 🔒 сесія недійсна — відкриваю вікно для логіну…")
        browser.close()
        _do_login_flow_global(p, profile_dir, start_url, stock_name, wait_cond)
        browser, pw_page = _open()
        return True

    browser, pw_page = _open()

    # Trigger 1: navigated straight onto a login page (Getty logs in inside its own
    # collector, so it's exempt from this generic landing check).
    if _is_login_url(pw_page.url) and stock_name != "Getty Images":
        if not _needs_login_now():
            return browser, pw_page

    # Trigger 2: collector reports it can't authenticate / is blocked.
    result = _dispatch_collector(stock_name, pw_page)
    if result == "needs_login":
        if not _needs_login_now():
            return browser, pw_page
        _dispatch_collector(stock_name, pw_page)   # retry once in the fresh session

    # Refresh the macOS cookie cache from this live context so the NEXT sync can use
    # the fast direct-API path instead of opening a browser again.
    try:
        from cookies import _capture_pw_cookies
        _capture_pw_cookies(browser)
    except Exception:
        pass
    return browser, pw_page


def _collect_one_stock_global(name, url, allow_login=False):
    """Збирає один сток у власному sync_playwright контексті.
    allow_login=True only for an explicit single-stock request (a stock button);
    Sync All passes False so missing logins are logged, not popped as windows."""
    from playwright.sync_api import sync_playwright as _spw
    import shutil

    # ── The ONE collection mechanism (macOS == Windows scheme) ────────────────
    # Cookies from a NATIVE Chrome login (real human session, NO Playwright/CDP →
    # passes PerimeterX/DataDome) → direct HTTPS requests. Every stock except
    # Alamy lives here. Flow: monthly gate → direct collect → if the session is
    # invalid (False) open a native Chrome login window → retry direct once.
    # NEVER falls back to Playwright. Adding a stock = *_collect_direct() + one
    # line in _DIRECT_COLLECTORS.
    if name in _DIRECT_COLLECTORS:
        if _monthly_gate_skip(name):
            return
        import importlib
        mod, fn = _DIRECT_COLLECTORS[name].rsplit(":", 1)
        direct_fn = getattr(importlib.import_module(mod), fn)
        try:
            res = direct_fn()
        except Exception as ex:
            _sync_log(f"[{name}] direct exception: {ex}")
            return   # transient/unknown failure — don't pop a login window
        if res == "transient":
            _sync_log(f"[{name}] ⏸ тимчасова помилка — повтор наступного синку")
            return
        if res:
            _mark_monthly_synced(name)
            return
        if not allow_login:
            _sync_log(f"[{name}] 🔒 потрібен логін — натисни кнопку «{name}»")
            return
        if _sync_stop_flag[0]:
            return
        from cookies import _native_chrome_login
        prof = os.path.join(_BASE_DIR, f"chrome_profile_native_{name.replace(' ', '_')}")
        login_url = _DIRECT_LOGIN_URLS.get(name, url)
        with _login_lock:          # one login window at a time (parallel Sync All)
            if _sync_stop_flag[0]:
                return
            _native_chrome_login(prof, login_url, name)
        try:
            res = direct_fn()
        except Exception as ex:
            _sync_log(f"[{name}] direct retry exception: {ex}")
            return
        if res is True or (res and res != "transient"):
            _mark_monthly_synced(name)
        return   # never fall through to Playwright for native-direct stocks

    # ── Playwright path — ONLY Alamy (ASP.NET UpdatePanel pagination) ─────────
    main_prof  = os.path.join(_BASE_DIR, "chrome_profile")
    safe = name.replace(" ", "_")
    stock_profile = os.path.abspath(f"chrome_profile_{safe}")
    if not os.path.exists(stock_profile) and os.path.exists(main_prof):
        _sync_log(f"[{name}] 📋 Copying profile...")
        _skip = {"SingletonSocket","SingletonLock","SingletonCookie","RunningChromeVersion"}
        shutil.copytree(main_prof, stock_profile,
                        ignore=lambda d, files: [f for f in files if f in _skip])
    # remove stale lock files so Chrome doesn't create a temp profile
    for _lf in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        _lp = os.path.join(stock_profile, _lf)
        if os.path.exists(_lp):
            try: os.remove(_lp)
            except Exception: pass
    try:
        with _spw() as _p:
            from sync_state import get_headless
            headless = get_headless()
            _sync_log(f"[{name}] ⏳ Starting...")
            browser, _ = _run_collector_global(_p, stock_profile, name, url, headless, allow_login)
            _sync_log(f"[{name}] ✅ Done")
            browser.close()
    except Exception as ex:
        _sync_log(f"[{name}] 🛑 Error: {ex}")


def _sync_all_global():
    """Parallel sales-stock collection, then MS+ at the end.

    All collectors now use direct HTTPS (no Playwright browsers), so the old
    fd-exhaustion concern that forced sequential mode is gone. Sales stocks
    run in parallel (4 workers) so user sees new sales fast. MS+ runs LAST
    because it's slow (15k photos) and not needed before sales are visible —
    only required before rebuild-matches at the end.

    SQLite contention is handled by WAL mode + timeout=15-60s on every conn.

    ⚠️ INVARIANTS — DO NOT BREAK:
    - Cold-start (DB <1000 rows): SEQUENTIAL mode. 4 parallel collectors on
      empty DB exhaust file descriptors / saturate rate limits.
    - MS+ ALWAYS runs after the 4 sales stocks finish (not parallel with them).
    - _session_new_keys.clear() at the START — this is what enables blue
      highlights for new sales (client reads via /api/sync/recent-keys)."""
    if _sync_all_active[0]:
        _sync_log("⚠️ Sync already running!")
        return
    _sync_all_active[0] = True
    _sync_stop_flag[0]  = False
    _sync_state["running"] = True
    _sync_state["log"]     = []
    with _session_new_keys_lock:
        _session_new_keys.clear()
    from sync_state import _begin_sync_batch
    _begin_sync_batch()   # one batch number for the whole run (all stocks)
    # First-run safeguard: empty DB → sequential mode. From-scratch sync hits
    # the full backfill path in every collector (Adobe = 2900+ pages, SS = days
    # since 2018, Getty = all 63 statements + ESP thumbs). 4 parallel doing
    # backfill at once exhausts fds / saturates SQLite / rate-limits stocks.
    # Once DB has data, incremental syncs are tiny and parallel is safe.
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as _c:
            _sales_count = _c.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    except Exception:
        _sales_count = 0
    parallel = _sales_count > 1000   # arbitrary threshold — well past any cold start

    if parallel:
        _sync_log("🚀 Parallel sync: 4 sales stocks at once, then MS+...")
    else:
        _sync_log(f"🚀 Sequential sync (DB has {_sales_count} rows — cold start safeguard)")

    SALES_STOCKS = ["Depositphotos", "Envato", "Freepik", "123RF", "PIXTA", "Dreamstime", "Alamy", "Getty Images", "Shutterstock", "Adobe Stock"]

    def _run_one(name):
        if _sync_stop_flag[0]:
            return
        url = STOCK_URLS.get(name)
        if not url:
            return
        _sync_log(f"━━━ [{name}] start ━━━")
        try:
            # allow_login=True: an expired session auto-opens a login window for
            # that stock (user request 2026-06-13) instead of silently skipping.
            _collect_one_stock_global(name, url, allow_login=True)
            _sync_log(f"━━━ [{name}] finished ━━━")
        except Exception as ex:
            _sync_log(f"━━━ [{name}] ⚠️ exception: {ex} ━━━")

    try:
        # Phase 1: sales stocks (parallel if warm DB, sequential if cold)
        if parallel:
            from concurrent.futures import ThreadPoolExecutor as _SyncTPE
            with _SyncTPE(max_workers=4) as pool:
                futures = [pool.submit(_run_one, n) for n in SALES_STOCKS]
                for f in futures:
                    try: f.result()
                    except Exception as ex: _sync_log(f"⚠️ sales worker: {ex}")
        else:
            for n in SALES_STOCKS:
                if _sync_stop_flag[0]: break
                _run_one(n)

        # Phase 2: MS+ (slow, runs alone, needed before matching)
        if not _sync_stop_flag[0]:
            _run_one("Microstock+")

        if not _sync_stop_flag[0]:
            _sync_log("✅ All stocks collected")
            # Backfill thumbnails for any sales photo missing from img_cache so pHash
            # matching works for all stocks (SS/Adobe/iStock/etc. with missing thumbs).
            try:
                from app_globals import CACHE_DIR
                from image_utils import load_img, load_match_thumb
                from concurrent.futures import ThreadPoolExecutor, wait as _fwait
                cached_set = set(f[:-4] for f in os.listdir(CACHE_DIR) if f.endswith('.jpg')) if os.path.isdir(CACHE_DIR) else set()
                with sqlite3.connect(DB_NAME, timeout=15) as _c:
                    missing = _c.execute(
                        "SELECT DISTINCT asset_id, stock, thumb_url FROM sales "
                        "WHERE thumb_url IS NOT NULL AND thumb_url != '' "
                        "AND asset_id NOT IN (SELECT asset_id FROM asset_meta "
                        "                     WHERE asset_id IS NOT NULL)"
                    ).fetchall()

                # Download SYNCHRONOUSLY (own pool + wait) — compute-hashes and
                # rebuild-matches below must see these files, otherwise the photos
                # only cluster on the NEXT sync (the "groups one sync late" bug).
                def _bf_one(aid, stock, turl):
                    if _sync_stop_flag[0]:
                        return
                    if stock == 'Adobe Stock':
                        load_img(aid, turl)            # display thumb (no meta)
                        load_match_thumb(aid, turl)    # clean thumb → asset_meta
                    else:
                        load_img(aid, turl, stock=stock)

                todo = [(a, s, t) for a, s, t in missing if a not in cached_set]
                if todo:
                    _sync_log(f"🖼️ Backfill: {len(todo)} missing thumbnails, downloading…")
                    with ThreadPoolExecutor(max_workers=10) as _bfp:
                        futs = [_bfp.submit(_bf_one, a, s, t) for a, s, t in todo]
                        _fwait(futs, timeout=600)
                    _sync_log(f"🖼️ Backfill done ({len(todo)})")
            except Exception as _ex:
                _sync_log(f"⚠️ Thumbnail backfill error: {_ex}")
            # Compute pHash for any cached thumbnails that still lack asset_meta rows.
            try:
                from main import flask_app
                from routes.matching import api_compute_hashes
                with flask_app.app_context():
                    r = api_compute_hashes()
                    computed = r.get_json().get('computed', 0)
                    if computed:
                        _sync_log(f"🔢 Computed pHash for {computed} cached thumbnails")
            except Exception as _ex:
                _sync_log(f"⚠️ compute-hashes error: {_ex}")
            try:
                _sync_log("🔗 Auto: rebuilding cross-stock matches...")
                from main import flask_app
                from routes.matching import api_rebuild_matches
                with flask_app.app_context():
                    resp = api_rebuild_matches()
                _sync_log(f"✅ Matches rebuilt: {resp.get_json()}")
            except Exception as ex:
                _sync_log(f"⚠️ rebuild-matches failed: {ex}")
        else:
            _sync_log("⛔ Sync stopped by user")
    except Exception as ex:
        _sync_log(f"🛑 Error: {ex}")
    finally:
        _sync_all_active[0]    = False
        _sync_stop_flag[0]     = False
        _sync_state["running"] = False

