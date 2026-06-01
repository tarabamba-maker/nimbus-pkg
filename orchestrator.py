"""
orchestrator.py — Sync orchestration: run collectors, coordinate parallel/sequential sync.

Moved here from main.py (logic unchanged — only location changed):
  - _run_collector_global   — opens Playwright browser + dispatches to stock collector
  - _collect_one_stock_global — direct API fast path → Playwright fallback per stock
  - _sync_all_global        — parallel sales sync then MS+, then rebuild-matches
"""

import json
import os
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
from collectors.adobe import _adobe_collect_direct, _adobe_api_collect_global
from collectors.shutterstock import _shutterstock_api_collect_direct, _shutterstock_api_collect_global
from collectors.getty import _getty_collect_direct, _getty_api_collect_global
from collectors.depositphotos import _depositphotos_collect
from collectors.ms_plus import _ms_plus_collect_direct, _ms_plus_collect_global

_BASE_DIR = os.environ.get("STOCK_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
RECIPES_DIR          = os.path.join(_BASE_DIR, "recipes")
MS_LIBRARY_FILE      = os.path.join(RECIPES_DIR, "ms_library.json")
PROCESSED_DATES_FILE = os.path.join(RECIPES_DIR, "_processed_dates.json")

STOCK_URLS = {
    "Adobe Stock":   "https://contributor.stock.adobe.com/en/sales",
    "Shutterstock":  "https://submit.shutterstock.com/earnings",
    "Getty Images":  "https://esp.gettyimages.com/contribute/stats",
    "Depositphotos": "https://depositphotos.com/account/sales-history.html",
    "Microstock+":   "https://microstock.plus/myfiles",
}

# _depositphotos_collect_direct is not yet implemented — placeholder
def _depositphotos_collect_direct():
    return False


def _run_collector_global(p, profile_dir, stock_name, start_url, headless, allow_login=False):
    """Відкриває браузер і запускає потрібний колектор.
    allow_login=True (single-stock button) opens a login window if the session
    expired; allow_login=False (Sync All) just logs that the stock needs login
    and skips it — so unwanted stocks don't pop windows on every full sync."""
    wait_cond = "networkidle" if stock_name == "Shutterstock" else "domcontentloaded"
    # Shutterstock: use real Chrome (channel='chrome') instead of bundled Chromium.
    # DataDome blocks the Playwright Chromium build by fingerprint; system Chrome
    # passes through normally.
    channel = "chrome" if stock_name == "Shutterstock" else None
    browser = _open_browser_context(p, profile_dir, headless, channel=channel)
    _apply_stealth(browser)
    pw_page = browser.pages[0] if browser.pages else browser.new_page()
    pw_page.goto(start_url, wait_until=wait_cond, timeout=60000)
    time.sleep(3 if stock_name != "Shutterstock" else 5)

    # Getty має свою login-логіку всередині колектора (чекає у тому самому вікні).
    # Не закриваємо браузер — просто йдемо далі.
    if _is_login_url(pw_page.url) and stock_name != "Getty Images":
        if not allow_login:
            _sync_log(f"[{stock_name}] 🔒 потрібен логін — натисни кнопку «{stock_name}» щоб увійти")
            return browser, pw_page
        browser.close()
        _do_login_flow_global(p, profile_dir, start_url, stock_name, wait_cond)
        browser = _open_browser_context(p, profile_dir, headless)
        _apply_stealth(browser)
        pw_page = browser.pages[0] if browser.pages else browser.new_page()
        pw_page.goto(start_url, wait_until=wait_cond, timeout=60000)
        time.sleep(3 if stock_name != "Shutterstock" else 5)

    if stock_name == "Adobe Stock":
        result = _adobe_api_collect_global(pw_page)
        if result == "needs_login":
            if not allow_login:
                _sync_log(f"[{stock_name}] 🔒 потрібен логін — натисни кнопку «{stock_name}» щоб увійти")
                return browser, pw_page
            browser.close()
            _do_login_flow_global(p, profile_dir, start_url, stock_name, wait_cond)
            browser = _open_browser_context(p, profile_dir, headless)
            _apply_stealth(browser)
            pw_page = browser.pages[0] if browser.pages else browser.new_page()
            pw_page.goto(start_url, wait_until="networkidle", timeout=60000)
            time.sleep(3)
            _adobe_api_collect_global(pw_page)
    elif stock_name == "Shutterstock":
        _shutterstock_api_collect_global(pw_page)
    elif stock_name == "Depositphotos":
        _depositphotos_collect(pw_page)
    elif stock_name == "Getty Images":
        # Auto-force on empty DB: pulls every available TSV statement instead of
        # waiting for the monthly-21st gate.
        try:
            with sqlite3.connect(DB_NAME, timeout=15) as _c:
                _g_count = _c.execute(
                    "SELECT COUNT(*) FROM sales WHERE stock IN ('iStock','iStockphoto','Getty Images')"
                ).fetchone()[0]
        except Exception:
            _g_count = 0
        _getty_api_collect_global(pw_page, force=(_g_count == 0))
    elif stock_name == "Microstock+":
        _ms_plus_collect_global(pw_page)
    return browser, pw_page


def _collect_one_stock_global(name, url, allow_login=False):
    """Збирає один сток у власному sync_playwright контексті.
    allow_login=True only for an explicit single-stock request (a stock button);
    Sync All passes False so missing logins are logged, not popped as windows."""
    from playwright.sync_api import sync_playwright as _spw
    import shutil

    # ⚠️ DO NOT REMOVE THIS FAST PATH — direct-API collectors using Safari cookies
    # are 10-100x faster than Playwright. They bypass DataDome because the datadome
    # trust token from the user's daily browsing carries over the request.
    # If the fast path returns True, we MUST skip the Playwright fallback for this
    # stock — otherwise we'd open a browser unnecessarily and risk re-triggering
    # anti-bot detection on a clean session. Each fast-path collector internally
    # falls back (returns False) if cookies missing/expired → Playwright kicks in.
    if name == "Adobe Stock":
        try:
            if _adobe_collect_direct():
                return
            _sync_log("[Adobe Stock] direct API failed — fallback to Playwright")
        except Exception as ex:
            _sync_log(f"[Adobe Stock] direct API exception: {ex} — fallback")
    if name == "Depositphotos":
        try:
            if _depositphotos_collect_direct():
                return
            _sync_log("[Depositphotos] direct failed — fallback to Playwright")
        except Exception as ex:
            _sync_log(f"[Depositphotos] direct exception: {ex} — fallback")
    if name == "Shutterstock":
        try:
            if _shutterstock_api_collect_direct():
                return
            _sync_log("[Shutterstock] direct API failed — fallback to Playwright")
        except Exception as ex:
            _sync_log(f"[Shutterstock] direct API exception: {ex} — fallback")
    if name == "Microstock+":
        try:
            if _ms_plus_collect_direct():
                return
            _sync_log("[Microstock+] direct API failed — fallback to Playwright")
        except Exception as ex:
            _sync_log(f"[Microstock+] direct API exception: {ex} — fallback")
    if name == "Getty Images":
        # Getty has the 21st-of-month gate (no point running before then)
        from datetime import date as _date_g
        if _date_g.today().day < 21:
            _sync_log(f"📅 [Getty Images] skipped — available from the 21st (today {_date_g.today().day})")
            return
        try:
            if _getty_collect_direct():
                return
            _sync_log("[Getty Images] direct API failed — fallback to Playwright")
        except Exception as ex:
            _sync_log(f"[Getty Images] direct API exception: {ex} — fallback")

    is_getty   = name == "Getty Images"
    main_prof  = os.path.join(_BASE_DIR, "chrome_profile")

    # Getty guard: stats publish around the 21st each month.
    # 1) Block before the 21st of the current month.
    # 2) Block after a successful sync until the 21st of the NEXT month.
    if is_getty:
        from datetime import date as _date
        today = _date.today()
        if today.day < 21:
            _sync_log(f"📅 [{name}] skipped — available from the 21st (today is the {today.day}th)")
            return
        # Check if already synced this month cycle
        try:
            with open(PROCESSED_DATES_FILE) as _pf:
                _pd_g = json.load(_pf)
        except Exception:
            _pd_g = {}
        last_sync_str = _pd_g.get('Getty/iStock_last_sync', '')
        if last_sync_str:
            try:
                last = _date.fromisoformat(last_sync_str)
                # Next allowed = 21st of the month after last sync
                next_m = last.month % 12 + 1
                next_y = last.year + (1 if last.month == 12 else 0)
                next_allowed = _date(next_y, next_m, 21)
                if today < next_allowed:
                    _sync_log(f"📅 [{name}] already collected this cycle — next sync from {next_allowed.strftime('%d.%m.%Y')}")
                    return
            except Exception:
                pass

    if is_getty:
        stock_profile = os.path.abspath("getty_profile")
    else:
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
            from main import _headless_mode, _headless_lock
            with _headless_lock:
                headless = _headless_mode
            # First-time/rebuild mode: force visible browser + login wait for ALL stocks.
            # User just wiped DB → likely needs to verify logins everywhere.
            _db_is_empty = False
            try:
                with sqlite3.connect(DB_NAME, timeout=15) as _c:
                    _db_is_empty = _c.execute(
                        "SELECT 1 FROM sales LIMIT 1").fetchone() is None
            except Exception:
                pass
            # For empty DB: trigger login flow for ALL stocks including MS+.
            # ms_library.json absence is the empty-state indicator for MS+.
            _ms_empty = not os.path.exists(MS_LIBRARY_FILE)
            _should_login = (_db_is_empty and name != "Microstock+") or \
                            (name == "Microstock+" and _ms_empty)
            if _should_login and not allow_login:
                _sync_log(f"[{name}] 🔒 не залогінено — натисни кнопку «{name}» щоб увійти (пропускаю у Sync All)")
                return
            if _should_login:
                _sync_log(f"[{name}] 🖥️ перший запуск — видимий браузер, чекаю поки залогінишся")
                # Force pre-login flow: opens visible browser, waits for user to close window
                _do_login_flow_global(_p, stock_profile,
                    "https://contributor.stock.adobe.com/en/insights/sales-earnings" if name == "Adobe Stock"
                    else "https://submit.shutterstock.com/earnings" if name == "Shutterstock"
                    else "https://depositphotos.com/account/sales-history.html" if name == "Depositphotos"
                    else "https://accountmanagement.gettyimages.com/Reports/Export" if is_getty
                    else "https://microstock.plus/myfiles" if name == "Microstock+"
                    else url,
                    name,
                    "networkidle" if name == "Shutterstock" else "domcontentloaded")
                headless = True  # after login confirmed, run actual sync headless
            # Getty: видимий тільки якщо сесія протухла (нема ccw cookie у профілі).
            # Якщо cookie валідна — синк у фоні (headless).
            if is_getty and not _db_is_empty:
                cookies_db = os.path.join(stock_profile, "Default", "Cookies")
                has_ccw = False
                try:
                    import sqlite3 as _sql3
                    con = _sql3.connect(f"file:{cookies_db}?mode=ro", uri=True, timeout=2)
                    row = con.execute(
                        "SELECT 1 FROM cookies WHERE name='ccw' AND expires_utc > 0 LIMIT 1"
                    ).fetchone()
                    has_ccw = row is not None
                    con.close()
                except Exception:
                    has_ccw = False
                headless = has_ccw
                _sync_log(f"[{name}] {'🤖 headless (сесія активна)' if has_ccw else '🖥️ видимий (потрібен логін)'}")
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

    SALES_STOCKS = ["Depositphotos", "Getty Images", "Shutterstock", "Adobe Stock"]

    def _run_one(name):
        if _sync_stop_flag[0]:
            return
        url = STOCK_URLS.get(name)
        if not url:
            return
        _sync_log(f"━━━ [{name}] start ━━━")
        try:
            _collect_one_stock_global(name, url)
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
            try:
                _sync_log("🔗 Auto: rebuilding cross-stock matches...")
                from main import flask_app, api_rebuild_matches
                with flask_app.test_request_context():
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

