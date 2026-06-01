import ssl, time, threading, sqlite3, os, re, json, base64, atexit, sys
from io import BytesIO
from datetime import datetime, timedelta

from utils import (
    _adobe_clean_thumb_url,
    _hamming_hex,
    _dhash_from_path,
    _dpapi_unprotect,
)
from db import init_db, is_already_saved, save_to_db
from sync_state import (
    _sync_state, _sync_stop_flag, _sync_all_active,
    _session_new_keys, _session_new_keys_lock, _sync_log_lock,
    _sync_log, _save_record,
)
from collectors.browser import (
    _STEALTH_JS, _apply_stealth,
    _LOGIN_SIGNALS, _is_login_url,
    _open_browser_context, _do_login_flow_global,
)
from collectors.adobe import _adobe_collect_direct, _adobe_api_collect_global
from collectors.shutterstock import _shutterstock_api_collect_direct, _shutterstock_api_collect_global
from collectors.getty import _getty_collect_direct, _getty_api_collect_global
from collectors.depositphotos import _depositphotos_collect
from collectors.ms_plus import _ms_plus_collect_direct, _ms_plus_collect_global
from image_utils import (
    _save_asset_meta, load_img, load_match_thumb, load_img_async,
    _img_executor, _http_executor,
    load_groups, save_groups, load_ms_library, save_ms_library,
)
from matching_engine import (
    _hash_based_matches, _ms_fname_to_libentry,
    _ms_visual_matches, _filename_fallback_matches,
    _apply_manual_overrides,
)
from cookies import (
    _is_chrome_running, _chrome_cookies_path,
    _decrypt_chrome_cookie_db, _load_chrome_cookies_windows,
    _load_appprofile_cookies_windows, _load_browser_cookies,
    _parse_safari_binarycookies,
    _inject_cookies_via_playwright,
    _STOCK_COOKIE_DOMAINS, _import_cookies_for_stock,
    _is_safari_running, _detect_default_browser,
    _STOCK_LOGIN_URLS, _stock_profile_dir,
    _clear_profile_locks, _windows_browser_login,
)

# Platform: 'darwin' (macOS), 'win32' (Windows), 'linux'.
# Used for browser-cookie source selection (Safari on mac vs Chrome on Windows).
IS_MAC = sys.platform == 'darwin'
IS_WIN = sys.platform == 'win32'

# Windows consoles default to cp1252, which can't encode the emoji used in our
# log lines (🚀✅⚠️…) — any such print() would raise UnicodeEncodeError and crash
# the backend at startup. Force UTF-8 on stdout/stderr (no-op on macOS).
if IS_WIN:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

# Windows-only kwargs for subprocess to suppress cmd-window flashes.
# subprocess.CREATE_NO_WINDOW = 0x0800_0000. We pass as creationflags.
_SUBPROC_NOWINDOW = (
    {'creationflags': 0x0800_0000} if IS_WIN else {}
)

ssl._create_default_https_context = ssl._create_unverified_context

# Raise file descriptor limit. macOS default soft limit is 256, hard is
# "unlimited" but actually capped at OPEN_MAX (typically 10240 or higher).
# With 5 parallel direct-API collectors each holding HTTPS connection pool +
# SQLite connections + img_executor threads, 4096 wasn't enough → "Too many
# open files" mid-sync killed Getty's DB writes. Try 65536, fall back stepwise.
# Unix only — `resource` module doesn't exist on Windows (fds managed differently).
if not IS_WIN:
    try:
        import resource as _res
        _, _hard = _res.getrlimit(_res.RLIMIT_NOFILE)
        for _target in (65536, 32768, 16384, 8192, 4096):
            try:
                _res.setrlimit(_res.RLIMIT_NOFILE, (_target, _hard if _hard >= _target else _target))
                break
            except Exception:
                continue
    except Exception:
        pass

# ── Логування у файл + термінал ──────────────────────────────
# Log MUST go to a user-writable directory. On Windows the packaged app sits
# inside Program Files which is read-only — silent open() failure left the log
# empty and made debugging impossible. Prefer STOCK_DATA_DIR (set by Tauri),
# fall back to script dir for dev runs.
_LOG_DIR  = os.environ.get("STOCK_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
try: os.makedirs(_LOG_DIR, exist_ok=True)
except Exception: pass
_LOG_FILE = os.path.join(_LOG_DIR, "app.log")
try:
    _log_file_handle = open(_LOG_FILE, "a", encoding="utf-8", errors="replace", buffering=1)
except Exception:
    # Last-resort: temp dir. We'd rather have a log than nothing.
    import tempfile as _tf
    _LOG_FILE = os.path.join(_tf.gettempdir(), "stock_automation_app.log")
    _log_file_handle = open(_LOG_FILE, "a", encoding="utf-8", errors="replace", buffering=1)
atexit.register(lambda: _log_file_handle.close())

def _app_log(msg: str):
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try: print(line, flush=True)
    except Exception: pass
    try: _log_file_handle.write(line + "\n")
    except Exception: pass

# Завантажуємо .env
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# Автоматично встановлюємо Playwright браузери якщо їх немає
def _ensure_playwright_browsers():
    """Verify chromium binaries exist and match Playwright version. Old caches
    pass the executable_path check but fail on launch because the directory
    name encodes the version (chromium-XXXX, chromium_headless_shell-XXXX)."""
    import subprocess, sys
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as _pw:
            # Windows drives the user's system Chrome (channel='chrome') — never
            # bundle or download Playwright's Chromium. Just verify Chrome runs.
            if IS_WIN:
                browser = _pw.chromium.launch(headless=True, channel="chrome")
            else:
                # macOS: launch bundled chromium — catches version mismatch too.
                browser = _pw.chromium.launch(headless=True)
            browser.close()
            return
    except Exception as ex:
        if IS_WIN:
            print(f"[Playwright] System Chrome launch failed: {ex} — "
                  "встанови Google Chrome (він обов'язковий на Windows).")
            return
        print(f"[Playwright] Browser launch failed: {ex} — installing chromium + headless-shell...")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "chromium-headless-shell"],
            check=False, timeout=300, **_SUBPROC_NOWINDOW)
        print("[Playwright] ✅ Browsers installed")
    except Exception as e:
        print(f"[Playwright] ⚠️ Install failed: {e}")

_ensure_playwright_browsers()

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as req_lib
from PIL import Image, ImageOps

# Імпортуємо сучасний рушій браузера
try:
    from playwright.sync_api import sync_playwright
    HAS_PW = True
except ImportError:
    HAS_PW = False

# ═══════════════════════════════════════════════════════════
# КОНСТАНТИ
# ═══════════════════════════════════════════════════════════
# ⚠️ DO NOT REVERT _BASE_DIR to os.path.dirname(__file__).
# Tauri-packaged main.py lives INSIDE the .app bundle (.../Resources/_up_/_up_/),
# which is replaced wholesale on every auto-update. Storing sales.db there →
# all user data lost on update. STOCK_DATA_DIR points to a stable user-writable
# location (~/Library/Application Support/StockAutomation on Mac).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR   = os.environ.get("STOCK_DATA_DIR", _SCRIPT_DIR)
os.makedirs(_BASE_DIR, exist_ok=True)

# ⚠️ DO NOT DELETE THIS MIGRATION BLOCK.
# First launch on a fresh install (after the STOCK_DATA_DIR switch) needs to
# pull existing data from the old script-relative location and from the user's
# dev tree on Desktop. Safe to run repeatedly — it's a no-op if new dir already
# has sales.db.
def _migrate_from(old_dir):
    """Copy DB + recipes + caches + chrome profiles from old location if new dir is empty."""
    if not os.path.isdir(old_dir) or os.path.abspath(old_dir) == os.path.abspath(_BASE_DIR):
        return
    new_db = os.path.join(_BASE_DIR, "sales.db")
    old_db = os.path.join(old_dir, "sales.db")
    if os.path.exists(new_db) or not os.path.exists(old_db):
        return
    import shutil as _sh
    print(f"[migration] copying user data from {old_dir} → {_BASE_DIR}", flush=True)
    for name in ("sales.db", "recipes", "img_cache", "img_cache_match",
                 "img_cache_ms", "img_cache_icons", "ms_library.json"):
        src = os.path.join(old_dir, name)
        dst = os.path.join(_BASE_DIR, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            if os.path.isdir(src): _sh.copytree(src, dst)
            else:                  _sh.copy2(src, dst)
            print(f"[migration]   ✓ {name}", flush=True)
        except Exception as ex:
            print(f"[migration]   ✗ {name}: {ex}", flush=True)
    # Profiles: copy lazily — they're heavy. Just copy the chrome_profile dirs.
    for entry in os.listdir(old_dir):
        if entry.endswith("_profile") or entry.startswith("chrome_profile") or entry.startswith("getty_profile"):
            src = os.path.join(old_dir, entry)
            dst = os.path.join(_BASE_DIR, entry)
            if os.path.isdir(src) and not os.path.exists(dst):
                try:
                    _sh.copytree(src, dst)
                    print(f"[migration]   ✓ {entry}/", flush=True)
                except Exception as ex:
                    print(f"[migration]   ✗ {entry}/: {ex}", flush=True)

# Try common old locations
for _cand in (
    _SCRIPT_DIR,  # script's own dir (dev mode)
    os.path.expanduser("~/Desktop/Stock_Automation"),  # user's dev tree
):
    _migrate_from(_cand)

DB_NAME         = os.path.join(_BASE_DIR, "sales.db")
CACHE_DIR       = os.path.join(_BASE_DIR, "img_cache")
MATCH_CACHE_DIR = os.path.join(_BASE_DIR, "img_cache_match")
MS_CACHE_DIR    = os.path.join(_BASE_DIR, "img_cache_ms")
ICON_CACHE_DIR  = os.path.join(_BASE_DIR, "img_cache_icons")
FLASK_PORT      = 8000
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(MATCH_CACHE_DIR, exist_ok=True)
os.makedirs(MS_CACHE_DIR, exist_ok=True)
os.makedirs(ICON_CACHE_DIR, exist_ok=True)

# _adobe_clean_thumb_url → utils.py

RECIPES_DIR            = os.path.join(_BASE_DIR, "recipes")
MATCHES_FILE           = os.path.join(RECIPES_DIR, "_cross_stock_matches.json")
MANUAL_OVERRIDES_FILE  = os.path.join(RECIPES_DIR, "_manual_overrides.json")
GROUPS_FILE            = os.path.join(RECIPES_DIR, "photo_groups.json")
MS_LIBRARY_FILE        = os.path.join(RECIPES_DIR, "ms_library.json")
PROCESSED_DATES_FILE   = os.path.join(RECIPES_DIR, "_processed_dates.json")
os.makedirs(RECIPES_DIR, exist_ok=True)


def _load_matches() -> dict:
    try:
        with open(MATCHES_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_matches(m: dict):
    with open(MATCHES_FILE, "w") as f:
        json.dump(m, f, indent=2)

def _load_overrides() -> dict:
    """Manual user-edited match overrides. Structure:
       {"linked":   {primary_id: [forced_member_ids...]},
        "unlinked": {primary_id: [forced_removed_ids...]}}
       Applied AFTER all auto passes; always wins over auto-matching."""
    try:
        with open(MANUAL_OVERRIDES_FILE) as f:
            d = json.load(f)
            return {"linked": d.get("linked", {}), "unlinked": d.get("unlinked", {})}
    except Exception:
        return {"linked": {}, "unlinked": {}}

def _save_overrides(d: dict):
    with open(MANUAL_OVERRIDES_FILE, "w") as f:
        json.dump({"linked": d.get("linked", {}), "unlinked": d.get("unlinked", {})}, f, indent=2)

STOCKS = [
    "Adobe Stock", "Shutterstock", "Getty Images", "Depositphotos",
]

STOCK_URLS = {
    "Adobe Stock":   "https://contributor.stock.adobe.com/en/sales",
    "Shutterstock":  "https://submit.shutterstock.com/earnings",
    "Getty Images":  "https://esp.gettyimages.com/contribute/stats",
    "Depositphotos": "https://depositphotos.com/account/sales-history.html",
    "Microstock+":   "https://microstock.plus/myfiles",
}

# ═══════════════════════════════════════════════════════════
# БД
# ═══════════════════════════════════════════════════════════
_STOCK_KEY = {
    "Adobe Stock": "adobe", "Shutterstock": "shutterstock",
    "iStock": "istock", "iStockphoto": "istock",
    "Getty Images": "getty", "Depositphotos": "depositphotos",
    "Pond5": "pond5", "Alamy": "alamy",
}

# init_db → db.py
# is_already_saved → db.py
# save_to_db → db.py

# ═══════════════════════════════════════════════════════════
# FLASK
# ═══════════════════════════════════════════════════════════
flask_app = Flask(__name__)
# Allow large backup imports (default Flask limit ≈ 16 MB; our backups can be ~30 MB)
flask_app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB cap
CORS(flask_app)

# _save_record → sync_state.py

@flask_app.route('/update', methods=['POST'])
def api_update():
    d = request.json
    if not d: return jsonify({"status": "error"}), 400
    _save_record(d)
    return jsonify({"status": "success"}), 200

# ═══════════════════════════════════════════════════════════
# TAURI API — використовується Svelte UI
# ═══════════════════════════════════════════════════════════

# _sync_state, _sync_stop_flag, _sync_all_active → sync_state.py
# _session_new_keys, _session_new_keys_lock, _sync_log_lock → sync_state.py
# _sync_log → sync_state.py
# _save_record → sync_state.py
_headless_mode: bool   = True
_headless_lock = threading.Lock()


# ───────────────────────────────────────────────────────────
# Playwright-колектори (top-level, без Flet)
# ───────────────────────────────────────────────────────────

# _STEALTH_JS, _apply_stealth → collectors/browser.py
# _LOGIN_SIGNALS, _is_login_url → collectors/browser.py
# _open_browser_context → collectors/browser.py
# _do_login_flow_global → collectors/browser.py


# _adobe_collect_direct → collectors/adobe.py
# _adobe_api_collect_global → collectors/adobe.py


# _shutterstock_api_collect_direct → collectors/shutterstock.py
# _shutterstock_api_collect_global → collectors/shutterstock.py


# _depositphotos_collect → collectors/depositphotos.py
# _ms_plus_collect_direct → collectors/ms_plus.py
# _ms_plus_collect_global → collectors/ms_plus.py

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

@flask_app.route('/api/sales', methods=['GET'])
def api_sales():
    """Повертає агреговані продажі для Best Sellers tab."""
    from collections import defaultdict
    period  = request.args.get('period', 'All-time')
    stock   = request.args.get('stock', 'All')
    page_n  = int(request.args.get('page', 1))
    per_pg  = int(request.args.get('per_page', 50))
    q       = request.args.get('q', '').strip()
    sort_by = request.args.get('sort', 'total')  # 'total' | 'count'
    sort_dir = request.args.get('dir', 'desc')   # 'desc' | 'asc'

    now = datetime.now()
    cutoffs = {
        'Today': now.strftime('%Y-%m-%d'),
        'Week':  (now - timedelta(days=7)).strftime('%Y-%m-%d'),
        'Month': (now - timedelta(days=30)).strftime('%Y-%m-%d'),
        'Year':  (now - timedelta(days=365)).strftime('%Y-%m-%d'),
    }

    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        params = []
        where  = []
        if period in cutoffs:
            where.append("date >= ?"); params.append(cutoffs[period])
        if stock != 'All':
            where.append("stock = ?"); params.append(stock)
        if q:
            where.append("CAST(asset_id AS TEXT) LIKE ?"); params.append(f"%{q}%")
        ws = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT asset_id, stock, SUM(price), COUNT(*), MAX(thumb_url) "
            f"FROM sales {ws} GROUP BY asset_id, stock", params).fetchall()

    # Агрегація по кластерах (cross-stock matches)
    matches = _load_matches()
    aid_to_primary: dict = {}
    for primary, members in matches.items():
        for m in members:
            aid_to_primary[m] = primary

    agg = {}
    for asset_id, sk, row_total, row_count, thumb in rows:
        aid = str(asset_id)
        pr  = float(row_total or 0)
        key = aid_to_primary.get(aid, aid)
        if key in agg:
            agg[key]['total']   += pr
            agg[key]['count']   += row_count
            if not agg[key]['thumb_url'] and thumb: agg[key]['thumb_url'] = thumb
            agg[key]['by_stock'].setdefault(sk, {'total': 0.0, 'count': 0})
            agg[key]['by_stock'][sk]['total'] += pr
            agg[key]['by_stock'][sk]['count'] += row_count
        else:
            agg[key] = {'asset_id': key, 'total': pr, 'count': row_count,
                        'thumb_url': thumb or '', 'stock': sk,
                        'merged': False,
                        'by_stock': {sk: {'total': pr, 'count': row_count}}}

    for v in agg.values():
        v['merged'] = len(v['by_stock']) > 1

    sort_key  = 'count' if sort_by == 'count' else 'total'
    all_items = sorted(agg.values(), key=lambda x: x[sort_key], reverse=(sort_dir != 'asc'))
    total_sum = sum(v['total'] for v in agg.values())
    total_count = len(all_items)
    start = (page_n - 1) * per_pg
    page_items = all_items[start:start + per_pg]

    return jsonify({
        "total_sum": round(total_sum, 2),
        "total_count": total_count,
        "page": page_n,
        "per_page": per_pg,
        "items": page_items
    })

@flask_app.route('/api/feed', methods=['GET'])
def api_feed():
    """Повертає стрічку продажів для Downloads tab."""
    period = request.args.get('period', 'All-time')
    stock  = request.args.get('stock', 'All')
    page_n = int(request.args.get('page', 1))
    per_pg = int(request.args.get('per_page', 50))

    now = datetime.now()
    cutoffs = {
        'Today': now.strftime('%Y-%m-%d'),
        'Week':  (now - timedelta(days=7)).strftime('%Y-%m-%d'),
        'Month': (now - timedelta(days=30)).strftime('%Y-%m-%d'),
        'Year':  (now - timedelta(days=365)).strftime('%Y-%m-%d'),
    }

    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        params = []
        where  = []
        if period in cutoffs:
            where.append("date >= ?"); params.append(cutoffs[period])
        if stock != 'All':
            where.append("stock = ?"); params.append(stock)
        ws = ("WHERE " + " AND ".join(where)) if where else ""
        total_count = conn.execute(f"SELECT COUNT(*) FROM sales {ws}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, asset_id, price, thumb_url, date, stock "
            f"FROM sales {ws} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [per_pg, (page_n - 1) * per_pg]).fetchall()

    # Per-stock aggregates expanded via cross-stock matches
    aid_set = list(dict.fromkeys(str(r[0]) for r in rows))  # unique, preserves order
    by_stock_map = {}
    if aid_set:
        # Build a reverse-lookup from matches so we can find sibling IDs
        matches = _load_matches()
        rev_match: dict = {}
        for primary, members in matches.items():
            for m in members:
                rev_match[str(m)] = str(primary)

        # Expand query to include all matched sibling asset_ids
        expanded: set = set(aid_set)
        for aid in aid_set:
            primary = rev_match.get(aid, aid)
            group = matches.get(primary) or matches.get(aid)
            if group:
                expanded.update(str(m) for m in group)

        with sqlite3.connect(DB_NAME, timeout=15) as conn2:
            exp_list = list(expanded)
            ph2 = ','.join('?' * len(exp_list))
            agg = conn2.execute(
                f"SELECT asset_id, stock, SUM(price), COUNT(*), MIN(date) FROM sales"
                f" WHERE asset_id IN ({ph2})"
                f" GROUP BY asset_id, stock", exp_list).fetchall()

        # Raw per-id earnings
        raw_by_stock: dict = {}
        for r in agg:
            a = str(r[0])
            raw_by_stock.setdefault(a, {})[r[1]] = {
                "total": round(float(r[2] or 0), 2), "count": r[3] or 0}

        # Merge matched siblings into each feed item's by_stock
        for aid in aid_set:
            primary = rev_match.get(aid, aid)
            group = matches.get(primary) or matches.get(aid) or [aid]
            merged: dict = {}
            for m in group:
                m = str(m)
                for sk, sv in (raw_by_stock.get(m) or {}).items():
                    merged.setdefault(sk, {'total': 0.0, 'count': 0})
                    merged[sk]['total'] = round(merged[sk]['total'] + sv['total'], 2)
                    merged[sk]['count'] += sv['count']
            by_stock_map[aid] = merged
    items = [{'id': r[0], 'asset_id': str(r[1]), 'price': float(r[2] or 0),
              'thumb_url': r[3] or '', 'date': (r[4] or '')[:10],
              'stock': r[5] or '',
              'by_stock': by_stock_map.get(str(r[1]), {})} for r in rows]
    return jsonify({"total_count": total_count, "page": page_n,
                    "per_page": per_pg, "items": items})

@flask_app.route('/api/stats', methods=['GET'])
def api_stats():
    """Загальна статистика з delta. ?stock=All|Adobe Stock|Shutterstock|iStock"""
    now = datetime.now()
    stock_filter = request.args.get('stock', 'All')

    # Parameterized stock filter — no string interpolation of user input
    if stock_filter and stock_filter != 'All':
        base_where  = "stock = ?"
        base_params = [stock_filter]
    else:
        base_where  = "1=1"
        base_params = []

    today_d = now.date()
    week_start  = today_d - timedelta(days=today_d.weekday())   # Monday
    prev_week_start = week_start - timedelta(days=7)
    # Same DOW progress in prev week (excl) — Mon..today maps to Mon..today-7
    prev_week_end   = today_d - timedelta(days=7)
    month_start     = today_d.replace(day=1)
    # Same day-of-month in prev month, clamped if prev month is shorter
    _pm_last_day    = (month_start - timedelta(days=1))
    prev_month_start = _pm_last_day.replace(day=1)
    try:
        prev_month_end = prev_month_start.replace(day=today_d.day)
    except ValueError:
        prev_month_end = _pm_last_day  # e.g., 31 May → 30 Apr
    year_start      = today_d.replace(month=1, day=1)
    prev_year_start = year_start.replace(year=year_start.year - 1)
    try:
        prev_year_end = today_d.replace(year=today_d.year - 1)
    except ValueError:
        prev_year_end = today_d.replace(year=today_d.year - 1, day=28)
    yesterday       = today_d - timedelta(days=1)

    # Format: (current_from, current_to_exact_day_or_None, prev_from, prev_to_exclusive_or_None)
    # current_to is None → range "date >= cur_from"
    # prev_to is set → range "date >= prev_from AND date < prev_to" (same progress)
    spans = {
        'today': (today_d.strftime('%Y-%m-%d'),
                  yesterday.strftime('%Y-%m-%d'),
                  yesterday.strftime('%Y-%m-%d'), None),
        'week':  (week_start.strftime('%Y-%m-%d'), None,
                  prev_week_start.strftime('%Y-%m-%d'),
                  prev_week_end.strftime('%Y-%m-%d')),
        'month': (month_start.strftime('%Y-%m-%d'), None,
                  prev_month_start.strftime('%Y-%m-%d'),
                  prev_month_end.strftime('%Y-%m-%d')),
        'year':  (year_start.strftime('%Y-%m-%d'), None,
                  prev_year_start.strftime('%Y-%m-%d'),
                  prev_year_end.strftime('%Y-%m-%d')),
    }
    result = {}
    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        def q(extra_where, extra_params):
            r = conn.execute(
                f"SELECT SUM(price), COUNT(*) FROM sales WHERE {base_where} AND {extra_where}",
                base_params + extra_params).fetchone()
            return round(float(r[0] or 0), 2), r[1] or 0

        def q_stock(extra_where, extra_params):
            rows = conn.execute(
                f"SELECT stock, SUM(price), COUNT(*) FROM sales WHERE {base_where} AND {extra_where} GROUP BY stock",
                base_params + extra_params).fetchall()
            return {r[0]: {"total": round(float(r[1] or 0), 2), "count": r[2] or 0} for r in rows}

        for key, (cur_from, cur_to, prev_from, prev_to) in spans.items():
            if cur_to:
                # 'today' span: exact-day match. DB stores "YYYY-MM-DD HH:MM:SS",
                # so plain `date = '2026-05-21'` never matches — use LIKE with day prefix.
                cur_t, cur_c = q("date LIKE ?", [cur_from + '%'])
                prv_t, _     = q("date LIKE ?", [cur_to + '%'])
                ps = q_stock("date LIKE ?", [cur_from + '%'])
            else:
                cur_t, cur_c = q("date >= ?", [cur_from])
                # Same-progress comparison: prev period from prev_from to prev_to
                # (matches how far we are in the current period). Avoids unfair
                # partial-vs-full comparisons that always yielded negative deltas.
                prv_t, _     = q("date >= ? AND date < ?", [prev_from, prev_to])
                ps = q_stock("date >= ?", [cur_from])
            result[key] = {
                "total": cur_t, "count": cur_c,
                "delta": round(cur_t - prv_t, 2),
                "by_stock": ps,
            }

        row = conn.execute(
            f"SELECT SUM(price), COUNT(*) FROM sales WHERE {base_where}", base_params).fetchone()
        result['all'] = {"total": round(float(row[0] or 0), 2), "count": row[1] or 0, "delta": 0}

        # all-time per-stock for legend
        by_stock = conn.execute(
            "SELECT stock, SUM(price), COUNT(*) FROM sales GROUP BY stock"
        ).fetchall()
        result['by_stock'] = [{"stock": r[0], "total": round(float(r[1] or 0), 2),
                                "count": r[2]} for r in by_stock]
    return jsonify(result)

@flask_app.route('/api/stock-list', methods=['GET'])
def api_stock_list():
    """Повертає список стоків."""
    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        rows = conn.execute(
            "SELECT DISTINCT stock FROM sales WHERE stock IS NOT NULL ORDER BY stock"
        ).fetchall()
    stocks = [r[0] for r in rows if r[0]]
    # Ensure the three main stocks are always present in a consistent order
    preferred = ["Adobe Stock", "Shutterstock", "iStock"]
    result = [s for s in preferred if s in stocks]
    for s in stocks:
        if s not in result:
            result.append(s)
    if not result:
        result = preferred
    return jsonify(result)

_RELEVANT_STOCKS = {'adobestock', 'shutterstock', 'istock', 'esp', 'depositphotos'}

_DEFAULT_STOCK_COLORS = {
    'Adobe Stock':    '#f97316',
    'Shutterstock':   '#e11d48',
    'Getty Images':   '#a855f7',
    'iStock':         '#6366f1',
    'iStockphoto':    '#6366f1',
    'Depositphotos':  '#0061ff',
}
_STOCK_COLORS_FILE = os.path.join(RECIPES_DIR, 'stock_colors.json')

def _load_stock_colors():
    try:
        with open(_STOCK_COLORS_FILE) as f:
            saved = json.load(f)
        return {**_DEFAULT_STOCK_COLORS, **saved}
    except Exception:
        return dict(_DEFAULT_STOCK_COLORS)

def _save_stock_colors(colors):
    with open(_STOCK_COLORS_FILE, 'w') as f:
        json.dump(colors, f, indent=2)

@flask_app.route('/api/stock-colors', methods=['GET'])
def api_stock_colors_get():
    return jsonify(_load_stock_colors())

@flask_app.route('/api/stock-colors', methods=['POST'])
def api_stock_colors_set():
    data = request.get_json(force=True, silent=True) or {}
    colors = _load_stock_colors()
    colors.update(data)
    _save_stock_colors(colors)
    return jsonify({'ok': True})


# ── Export / Import ────────────────────────────────────────────────────────────
import zipfile, tempfile

@flask_app.route('/api/export', methods=['GET'])
def api_export():
    """Pack sales.db + recipes/ into a ZIP and stream it. img_cache/ excluded —
    it's regenerable from thumb_url in the DB and would inflate backup to ~250 MB."""
    try:
        # Use a real temp file, not BytesIO — img_cache aside, the recipes dir
        # contains ms_library.json (~15 MB) which would still bloat memory.
        # Also: send_file with BytesIO can return truncated streams on some paths.
        import tempfile as _tf
        tmp_fd, tmp_path = _tf.mkstemp(suffix='.zip', prefix='nimbus_backup_')
        os.close(tmp_fd)
        try:
            with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                if os.path.exists(DB_NAME):
                    # Use sqlite backup API to guarantee a consistent snapshot
                    # even if Flask is mid-write to sales.db.
                    db_snap = tmp_path + '.db'
                    src = sqlite3.connect(DB_NAME, timeout=15)
                    dst = sqlite3.connect(db_snap)
                    try:
                        src.backup(dst)
                    finally:
                        dst.close(); src.close()
                    zf.write(db_snap, 'sales.db')
                    os.remove(db_snap)
                if os.path.exists(RECIPES_DIR):
                    for root, _, files in os.walk(RECIPES_DIR):
                        for fname in files:
                            fpath = os.path.join(root, fname)
                            arcname = os.path.relpath(fpath, _BASE_DIR)
                            zf.write(fpath, arcname)
            size = os.path.getsize(tmp_path)
            _app_log(f"[export] backup size: {size} bytes")
            from flask import send_file
            # send_file with a real path lets Flask handle range requests + cleanup
            resp = send_file(tmp_path, mimetype='application/zip',
                             as_attachment=True,
                             download_name='stock_aggregator_backup.zip',
                             conditional=False)
            # Schedule temp file removal after response is sent
            @resp.call_on_close
            def _cleanup():
                try: os.remove(tmp_path)
                except Exception: pass
            return resp
        except Exception:
            try: os.remove(tmp_path)
            except Exception: pass
            raise
    except Exception as e:
        _app_log(f"[export] Error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@flask_app.route('/api/import-raw', methods=['POST'])
def api_import_raw():
    """Unpack raw-bytes ZIP body (Content-Type: application/zip) — simpler than
    multipart, used by Tauri's import flow that reads file via plugin-fs."""
    data = request.get_data()
    if not data:
        return jsonify({'ok': False, 'msg': 'Empty request body'}), 400
    tmp = tempfile.NamedTemporaryFile(suffix='.zip', delete=False).name
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
        return _do_import(tmp)
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except Exception: pass


def _do_import(zip_path):
    """Shared import logic: validates and extracts a backup ZIP to _BASE_DIR."""
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # Verify integrity first — testzip returns name of first bad file or None
            bad = zf.testzip()
            if bad:
                return jsonify({'ok': False, 'msg': f'Corrupted file in ZIP: {bad}'}), 400
            names = zf.namelist()
            if 'sales.db' not in names:
                return jsonify({'ok': False, 'msg': 'ZIP does not contain sales.db'}), 400
            for member in names:
                if member.startswith('/') or '..' in member.replace('\\', '/'):
                    return jsonify({'ok': False, 'msg': f'Unsafe path in ZIP: {member}'}), 400
            zf.extractall(_BASE_DIR)
        _app_log(f"[import] restored {len(names)} files to {_BASE_DIR}")
        return jsonify({'ok': True, 'files': len(names)})
    except zipfile.BadZipFile:
        return jsonify({'ok': False, 'msg': 'File is not a valid ZIP archive'}), 400
    except Exception as ex:
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'msg': str(ex)}), 500

def _query_earnings_batch(ids_iterable):
    """Query sales DB for a set of asset_ids; return earnings + thumb_map dicts."""
    earnings: dict = {}
    thumb_map: dict = {}
    ids_list = list(set(str(i) for i in ids_iterable if i))
    if not ids_list:
        return earnings, thumb_map
    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        for off in range(0, len(ids_list), 800):
            chunk = ids_list[off:off + 800]
            phs = ','.join('?' * len(chunk))
            rows = conn.execute(
                f"SELECT asset_id, stock, SUM(price), COUNT(*), MAX(thumb_url) FROM sales"
                f" WHERE asset_id IN ({phs})"
                f" GROUP BY asset_id, stock",
                chunk,
            ).fetchall()
            for r in rows:
                aid = str(r[0]); sk = r[1]; thumb = r[4]
                if aid not in earnings:
                    earnings[aid] = {'_total': 0.0, '_count': 0}
                earnings[aid]['_total'] += float(r[2] or 0)
                earnings[aid]['_count'] += r[3] or 0
                earnings[aid].setdefault(sk, {'total': 0.0, 'count': 0})
                earnings[aid][sk]['total'] += float(r[2] or 0)
                earnings[aid][sk]['count'] += r[3] or 0
                if thumb and aid not in thumb_map:
                    thumb_map[aid] = thumb
    return earnings, thumb_map


@flask_app.route('/api/groups', methods=['GET'])
def api_groups_get():
    """Groups from ms_library.json + photo_groups.json with earnings from DB.

    Optional query params:
      ?preview=N  — return only the top-N photos per group by earnings (default
                    returns all photos). Use preview=3 for fast list rendering;
                    full photos fetched per-group via /api/group-photos?name=…
    """
    try:
        preview_n = int(request.args.get('preview', 0))
    except ValueError:
        preview_n = 0
    # Groups come ONLY from photo_groups.json. ms_library.json is a separate
    # reference file (stockids for cross-stock lookup) — it does NOT define
    # which groups are visible. On Reset DB, photo_groups.json is deleted →
    # this returns []. On next sync, Pass D of rebuild-matches re-populates
    # photo_groups.json from ms_library.group field.
    user_groups = load_groups()
    if not user_groups:
        return jsonify([])

    lib = load_ms_library()
    # Index ms_library by any stockid → photo (for sibling lookup & thumbs)
    sid_to_photo: dict = {}
    for photo in lib:
        for k, v in (photo.get('stockids') or {}).items():
            if k in _RELEVANT_STOCKS and v:
                sid_to_photo[str(v)] = photo

    # Build ms_groups by walking user_groups; for each user-assigned primary,
    # find its ms_library entry and treat siblings (stockids) as members too.
    ms_groups: dict = {}
    for gname, aids in user_groups.items():
        ms_groups[gname] = []
        seen_keys: set = set()
        for aid in aids:
            photo = sid_to_photo.get(str(aid))
            if photo:
                key = id(photo)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                ms_groups[gname].append({
                    'filename': photo.get('filename', ''),
                    'ms_thumb': photo.get('thumb', ''),
                    'stockids': {k: str(v) for k, v in (photo.get('stockids') or {}).items()
                                 if k in _RELEVANT_STOCKS and v},
                })

    # Collect all IDs to query (group members + ms siblings)
    all_ids: set = set()
    for photos_raw in ms_groups.values():
        for ph in photos_raw:
            all_ids.update(ph['stockids'].values())
    for aids in user_groups.values():
        all_ids.update(str(a) for a in aids)

    earnings, thumb_map = _query_earnings_batch(all_ids)

    result = []

    # ── Process ms_library groups ────────────────────────────────────────
    for gname, photos_raw in ms_groups.items():
        g_total = 0.0; g_sales = 0; g_by_stock: dict = {}
        photos = []
        # Also merge any user-added photos for this group
        user_extra = [str(a) for a in user_groups.get(gname, [])]

        for ph in photos_raw:
            stockids = ph['stockids']
            ph_total = 0.0; ph_count = 0; ph_by_stock: dict = {}
            ph_thumb = ''; canonical_id = ''

            for sid in stockids.values():
                if not canonical_id:
                    canonical_id = sid
                e = earnings.get(sid)
                if not e:
                    continue
                ph_total += e['_total']
                ph_count += e['_count']
                if not ph_thumb and sid in thumb_map:
                    ph_thumb = thumb_map[sid]
                for sk, sv in e.items():
                    if sk.startswith('_'):
                        continue
                    ph_by_stock.setdefault(sk, {'total': 0.0, 'count': 0})
                    ph_by_stock[sk]['total'] += sv['total']
                    ph_by_stock[sk]['count'] += sv['count']

            if not canonical_id:
                continue
            if not ph_thumb:
                ph_thumb = ph.get('ms_thumb', '')
            g_total += ph_total; g_sales += ph_count
            for sk, sv in ph_by_stock.items():
                g_by_stock.setdefault(sk, {'total': 0.0, 'count': 0})
                g_by_stock[sk]['total'] += sv['total']
                g_by_stock[sk]['count'] += sv['count']
            photos.append({
                'asset_id':    canonical_id,
                'filename':    ph.get('filename', ''),
                'thumb':       ph_thumb,
                'earnings':    round(ph_total, 2),
                'sales_count': ph_count,
                'by_stock':    {k: {'total': round(v['total'], 2), 'count': v['count']}
                                for k, v in ph_by_stock.items()},
            })

        # All stockids already represented via ms_library (primary + siblings)
        represented: set = set()
        for ph_raw in photos_raw:
            represented.update(ph_raw['stockids'].values())
        for p in photos:
            represented.add(p['asset_id'])

        for aid in user_extra:
            if aid in represented:
                continue
            e = earnings.get(aid, {})
            ph_total = e.get('_total', 0.0); ph_count = e.get('_count', 0)
            ph_by_stock = {sk: sv for sk, sv in e.items() if not sk.startswith('_')}
            g_total += ph_total; g_sales += ph_count
            for sk, sv in ph_by_stock.items():
                g_by_stock.setdefault(sk, {'total': 0.0, 'count': 0})
                g_by_stock[sk]['total'] += sv['total']
                g_by_stock[sk]['count'] += sv['count']
            photos.append({
                'asset_id': aid, 'filename': aid,
                'thumb': thumb_map.get(aid, ''),
                'earnings': round(ph_total, 2), 'sales_count': ph_count,
                'by_stock': {k: {'total': round(v['total'], 2), 'count': v['count']}
                             for k, v in ph_by_stock.items()},
            })

        photos.sort(key=lambda x: -x['earnings'])
        result.append({
            'name': gname, 'count': len(photos),
            'total': round(g_total, 2), 'sales': g_sales,
            'by_stock': {k: {'total': round(v['total'], 2), 'count': v['count']}
                         for k, v in g_by_stock.items()},
            'photos': photos,
        })

    # ── Process user-only groups (not in ms_library) ─────────────────────
    ms_names = set(ms_groups.keys())
    for gname, aids in user_groups.items():
        if gname in ms_names or not aids:
            continue
        g_total = 0.0; g_sales = 0; g_by_stock: dict = {}
        photos = []
        for aid in aids:
            aid = str(aid)
            e = earnings.get(aid, {})
            ph_total = e.get('_total', 0.0); ph_count = e.get('_count', 0)
            ph_by_stock = {sk: sv for sk, sv in e.items() if not sk.startswith('_')}
            g_total += ph_total; g_sales += ph_count
            for sk, sv in ph_by_stock.items():
                g_by_stock.setdefault(sk, {'total': 0.0, 'count': 0})
                g_by_stock[sk]['total'] += sv['total']
                g_by_stock[sk]['count'] += sv['count']
            photos.append({
                'asset_id': aid, 'filename': aid,
                'thumb': thumb_map.get(aid, ''),
                'earnings': round(ph_total, 2), 'sales_count': ph_count,
                'by_stock': {k: {'total': round(v['total'], 2), 'count': v['count']}
                             for k, v in ph_by_stock.items()},
            })
        photos.sort(key=lambda x: -x['earnings'])
        result.append({
            'name': gname, 'count': len(photos),
            'total': round(g_total, 2), 'sales': g_sales,
            'by_stock': {k: {'total': round(v['total'], 2), 'count': v['count']}
                         for k, v in g_by_stock.items()},
            'photos': photos,
        })

    # ── MS+-only groups: in ms_library but not yet in photo_groups ───────────
    # Show groups even when no sales exist yet. Photos use basepath as pseudo-id
    # and ms_thumb as the thumbnail. Earnings = $0.
    represented_groups = set(g['name'] for g in result)
    ms_lib_groups: dict = {}
    for photo in lib:
        gname = photo.get('group', '')
        if gname and gname not in represented_groups:
            ms_lib_groups.setdefault(gname, []).append(photo)

    for gname, photos_raw in ms_lib_groups.items():
        photos = []
        for ph in photos_raw:
            bp = ph.get('basepath', '') or ph.get('filename', '')
            if not bp:
                continue
            photos.append({
                'asset_id':    bp,
                'filename':    ph.get('filename', ''),
                'thumb':       ph.get('thumb', ''),
                'earnings':    0.0,
                'sales_count': 0,
                'by_stock':    {},
            })
        if not photos:
            continue
        result.append({
            'name': gname, 'count': len(photos),
            'total': 0.0, 'sales': 0,
            'by_stock': {},
            'photos': photos,
        })

    result.sort(key=lambda x: x['total'], reverse=True)

    # Truncate photos list for preview mode — frontend fetches the full list
    # per group on demand via /api/group-photos.
    if preview_n > 0:
        for g in result:
            g['photos'] = g['photos'][:preview_n]
    return jsonify(result)


@flask_app.route('/api/group-photos', methods=['GET'])
def api_group_photos():
    """Return full photos list for a single group — used by the group modal.

    Loads only the requested group's photos, not the whole 100-group payload.
    """
    gname = (request.args.get('name') or '').strip()
    if not gname:
        return jsonify({'error': 'name required'}), 400

    lib = load_ms_library()
    photos_raw = [
        {
            'filename': p.get('filename', ''),
            'ms_thumb': p.get('thumb', ''),
            'stockids': {k: str(v) for k, v in p.get('stockids', {}).items()
                         if k in _RELEVANT_STOCKS and v},
        }
        for p in lib if (p.get('group') or '').strip() == gname
    ]
    user_groups = load_groups()
    user_extra = [str(a) for a in user_groups.get(gname, [])]

    all_ids: set = set()
    for ph in photos_raw:
        all_ids.update(ph['stockids'].values())
    all_ids.update(user_extra)
    earnings, thumb_map = _query_earnings_batch(all_ids)

    photos = []
    represented: set = set()

    for ph in photos_raw:
        stockids = ph['stockids']
        ph_total = 0.0; ph_count = 0; ph_by_stock: dict = {}
        ph_thumb = ''; canonical_id = ''
        for sid in stockids.values():
            represented.add(sid)
            if not canonical_id:
                canonical_id = sid
            e = earnings.get(sid)
            if not e:
                continue
            ph_total += e['_total']
            ph_count += e['_count']
            if not ph_thumb and sid in thumb_map:
                ph_thumb = thumb_map[sid]
            for sk, sv in e.items():
                if sk.startswith('_'):
                    continue
                ph_by_stock.setdefault(sk, {'total': 0.0, 'count': 0})
                ph_by_stock[sk]['total'] += sv['total']
                ph_by_stock[sk]['count'] += sv['count']
        if not canonical_id:
            continue
        if not ph_thumb:
            ph_thumb = ph.get('ms_thumb', '')
        photos.append({
            'asset_id':    canonical_id,
            'filename':    ph.get('filename', ''),
            'thumb':       ph_thumb,
            'earnings':    round(ph_total, 2),
            'sales_count': ph_count,
            'by_stock':    {k: {'total': round(v['total'], 2), 'count': v['count']}
                            for k, v in ph_by_stock.items()},
        })

    for aid in user_extra:
        if aid in represented:
            continue
        e = earnings.get(aid, {})
        ph_total = e.get('_total', 0.0); ph_count = e.get('_count', 0)
        ph_by_stock = {sk: sv for sk, sv in e.items() if not sk.startswith('_')}
        photos.append({
            'asset_id': aid, 'filename': aid,
            'thumb': thumb_map.get(aid, ''),
            'earnings': round(ph_total, 2), 'sales_count': ph_count,
            'by_stock': {k: {'total': round(v['total'], 2), 'count': v['count']}
                         for k, v in ph_by_stock.items()},
        })

    photos.sort(key=lambda x: -x['earnings'])
    return jsonify({'name': gname, 'photos': photos, 'count': len(photos)})


@flask_app.route('/api/photo-groups', methods=['GET'])
def api_photo_groups_get():
    """Returns merged groups: ms_library auto-groups + user photo_groups.json."""
    result: dict = {}

    # ms_library groups: all relevant stock IDs per group
    lib = load_ms_library()
    for photo in lib:
        gname = photo.get('group', '').strip()
        if not gname:
            continue
        if gname not in result:
            result[gname] = []
        for k, v in photo.get('stockids', {}).items():
            if k in _RELEVANT_STOCKS and v:
                sid = str(v)
                if sid not in result[gname]:
                    result[gname].append(sid)

    # user photo_groups.json (overlay / additions)
    for gname, aids in load_groups().items():
        if gname not in result:
            result[gname] = []
        for aid in aids:
            sid = str(aid)
            if sid not in result[gname]:
                result[gname].append(sid)

    return jsonify(result)

@flask_app.route('/api/photo-groups', methods=['POST'])
def api_photo_groups_post():
    """Оновлює ручні групи."""
    data = request.get_json(force=True, silent=True) or {}
    save_groups(data)
    return jsonify({"status": "ok"})

@flask_app.route('/api/photo-groups/<name>', methods=['DELETE'])
def api_photo_groups_delete(name):
    """Видаляє одну групу за назвою."""
    groups = load_groups()
    if name in groups:
        del groups[name]
        save_groups(groups)
    # Also clear the group field in ms_library.json so the group doesn't reappear
    lib = load_ms_library()
    changed = False
    for photo in lib:
        if photo.get('group') == name:
            photo['group'] = ''
            changed = True
    if changed:
        save_ms_library(lib)
    return jsonify({"status": "ok"})

@flask_app.route('/api/photo-groups/rename', methods=['POST'])
def api_photo_groups_rename():
    """Перейменовує групу: {old_name, new_name}."""
    data = request.get_json(force=True, silent=True) or {}
    old, new = data.get('old_name', ''), data.get('new_name', '').strip()
    if not old or not new:
        return jsonify({"status": "error", "msg": "missing names"}), 400
    groups = load_groups()
    if old not in groups:
        return jsonify({"status": "error", "msg": "group not found"}), 404
    if new in groups:
        return jsonify({"status": "error", "msg": "name taken"}), 409
    groups[new] = groups.pop(old)
    save_groups(groups)
    return jsonify({"status": "ok"})

# ═════════════════════════════════════════════════════════════════════════════
# ⚠️ MATCHING & GROUPING LOGIC — APPROVED 2026-05-27. DO NOT TOUCH WITHOUT USER OK.
# ═════════════════════════════════════════════════════════════════════════════
# This block (the next ~200 lines through api_match_within_group, plus the
# direct-API collectors above, plus api_rebuild_matches) implements the
# user's approved cross-stock matching pipeline. Tested end-to-end on real
# data after 2 days of iteration. Same pattern applies to any future stock:
#
#   1. Collector (per stock) fetches sales via Safari cookies → direct HTTPS
#      request (NOT Playwright). Saves rows with thumb_url. Each thumb
#      download triggers _save_asset_meta (pHash+RGB) via load_img_async(stock=).
#      For Adobe: clean thumb from img_cache_match/, NOT watermarked img_cache/.
#   2. MS+ collector populates ms_library.json (basepath-keyed, full stockids,
#      thumb downloaded to img_cache_ms/ + pHash via api_compute_ms_hashes).
#      Group name = LAST folder component (strip /2021/ prefix).
#   3. api_rebuild_matches runs 8 passes:
#        Pass 0: load _manual_overrides.json
#        Pass A: pHash+RGB cross-stock clustering (PRIMARY — works on clean thumbs)
#        Pass B: ms_library stockids FALLBACK (adds links pHash missed)
#        Pass C: filename fallback (camera-name collisions verified by pHash)
#        Pass D: sync ms_library.group → photo_groups (primary stockid per photo)
#        Pass E: auto-propagate groups via match clusters
#        Pass F: MS+ visual matching (img_cache_ms/ pHash ↔ sales asset_meta)
#        Pass G: dedup
#        Pass H: apply manual overrides (always win)
#   4. UI "Match Groups" button → api_match_similar_groups merges variant groups
#      (cropped/p1/p2/(N)) into canonical shortest name. Recolor stays separate.
#   5. UI per-card "Match more" button → api_match_within_group runs pHash
#      against one group's members to find ungrouped sales photos that fit.
#
# When adding a new stock, follow steps 1-2; rebuild-matches handles the rest.
# ═════════════════════════════════════════════════════════════════════════════

def _group_base_key(name: str) -> tuple:
    """⚠️ DO NOT TOUCH — group similarity matching rule (per user spec 2026-05-27).
    Extract a canonical key from group name so variants of the same shoot match.

    Match: '23-02-01 woman street' == '23-02-01 woman street cropped'
                                    == '23-02-01 woman street p1'
                                    == '23-02-01 woman street (159)'
                                    == '23-02-01 woman street p2 cropped'
    DON'T match: '23-02-01 woman street recolor' (recolor is intentionally different)

    Returns: (date_prefix, name_words_tuple) — None if no date prefix found.
    Two groups match iff their base keys are equal."""
    import re as _re
    if 'recolor' in name.lower():
        return None  # recolor variants stay separate
    # Date prefix: YYMMDD or YY-MM-DD at start (allowing optional leading path)
    # Examples: "23-02-01 woman street", "210510 Africa p1", "21-08-05 friends"
    s = name.strip()
    m = _re.match(r'(?:.*?[\\/])?(\d{2}-?\d{2}-?\d{2})\s+(.+?)$', s)
    if not m:
        return None
    date_norm = m.group(1).replace('-', '')   # 23-02-01 → 230201
    rest = m.group(2).lower()
    # Strip suffix variants: cropped, p1, p2, p3, (N)
    rest = _re.sub(r'\s*\((\d+)\)\s*', ' ', rest)         # remove "(159)"
    rest = _re.sub(r'\s+p\d+\b', '', rest)                # remove " p1", " p2"
    rest = _re.sub(r'\s+cropped\b', '', rest)             # remove " cropped"
    rest = _re.sub(r'\s+resize\b', '', rest)              # remove " resize"
    rest = _re.sub(r'\s+colour\b', '', rest)              # remove " colour"
    rest = _re.sub(r'\s+rec\b', '', rest)                 # remove trailing " rec"
    rest = _re.sub(r'\s+', ' ', rest).strip()             # collapse whitespace
    if not rest:
        return None
    return (date_norm, tuple(rest.split()))


@flask_app.route('/api/groups/match-similar', methods=['POST'])
def api_match_similar_groups():
    """⚠️ DO NOT TOUCH — merges variant groups of same shoot into canonical one.

    Per user spec (2026-05-27): groups like '23-02-01 woman street' +
    '23-02-01 woman street cropped' + '23-02-01 woman street p1' share the
    same shoot, must merge. Recolor stays separate (different processing).

    Logic:
      1. Compute _group_base_key for every group in photo_groups.json
      2. Bucket groups by identical base key
      3. For each bucket with ≥2 groups: pick canonical (shortest name, has no
         'cropped'/'p1'/etc suffix), merge others into it via existing merge.

    Does NOT touch ms_library — only photo_groups.json + matches."""
    groups = load_groups()
    buckets = {}
    for name in list(groups.keys()):
        key = _group_base_key(name)
        if not key: continue
        buckets.setdefault(key, []).append(name)

    merged_pairs = []
    skipped_recolor = 0
    for key, names in buckets.items():
        if len(names) < 2: continue
        # Pick canonical = shortest (most likely the original "23-02-01 woman street"
        # without "cropped"/"p1" suffix). Among equal-length: alphabetical first.
        names.sort(key=lambda n: (len(n), n))
        canonical = names[0]
        for src in names[1:]:
            # Merge src → canonical via existing logic (collects ms_library + user IDs)
            try:
                with flask_app.test_request_context(
                    json={'source': src, 'target': canonical}, method='POST'):
                    api_photo_groups_merge()
                merged_pairs.append((src, canonical))
            except Exception as ex:
                _app_log(f"[match-similar] merge {src}→{canonical} failed: {ex}")

    return jsonify({'status': 'ok',
                    'merged_count': len(merged_pairs),
                    'pairs': merged_pairs[:50],   # truncate for UI
                    'recolor_kept_separate': skipped_recolor})


@flask_app.route('/api/groups/match-within', methods=['POST'])
def api_match_within_group():
    """⚠️ DO NOT TOUCH — pHash matches all sales photos to a single group's
    visual reference. For each photo already in the group, find visually similar
    sales photos NOT yet in any group and add them in. Used by per-group "Match
    more" button in the card.

    Body: { "name": "group name" }
    Match thresholds: same as global _hash_based_matches (hamming ≤ 8, RGB ≤ 90)."""
    data = request.get_json(force=True, silent=True) or {}
    gname = (data.get('name') or '').strip()
    if not gname:
        return jsonify({'status': 'error', 'msg': 'missing name'}), 400

    groups = load_groups()
    if gname not in groups:
        return jsonify({'status': 'error', 'msg': 'group not found'}), 404

    member_ids = set(str(a) for a in groups[gname])
    if not member_ids:
        return jsonify({'status': 'ok', 'added': 0})

    # Build a set of all photos in ANY group (so we never poach grouped photos)
    all_grouped = set()
    for ids in groups.values():
        all_grouped.update(str(a) for a in ids)

    # Get pHash for member assets
    with sqlite3.connect(DB_NAME, timeout=15) as c:
        placeholders = ",".join(["?"] * len(member_ids))
        member_meta = c.execute(
            f"SELECT asset_id, thumb_hash, aspect_ratio, r, g, b FROM asset_meta "
            f"WHERE asset_id IN ({placeholders}) AND thumb_hash IS NOT NULL",
            list(member_ids)).fetchall()
        # All sales photos with pHash that are NOT in any group
        all_meta = c.execute(
            "SELECT stock, asset_id, thumb_hash, aspect_ratio, r, g, b FROM asset_meta "
            "WHERE thumb_hash IS NOT NULL").fetchall()

    if not member_meta:
        return jsonify({'status': 'ok', 'added': 0, 'msg': 'no member pHashes'})

    # Compare each ungrouped sales photo against EACH member's pHash
    member_ints = [(h, int(h, 16), ar, r, g, b) for h, ar, r, g, b
                   in [(m[1], m[2], m[3], m[4], m[5]) for m in member_meta]]
    added = []
    for stock, aid, ha, ar, r, g, b in all_meta:
        aid_s = str(aid)
        if aid_s in all_grouped: continue
        try: ha_int = int(ha, 16)
        except Exception: continue
        for mh, mh_int, mar, mr, mg, mb in member_ints:
            if ar and mar and abs(ar - mar) > 0.15: continue
            if bin(ha_int ^ mh_int).count('1') > 8: continue
            if (isinstance(r, int) and isinstance(mr, int) and
                abs(r - mr) + abs(g - mg) + abs(b - mb) > 90): continue
            added.append(aid_s); all_grouped.add(aid_s); break

    if added:
        groups[gname] = list(dict.fromkeys(list(groups[gname]) + added))
        save_groups(groups)

    return jsonify({'status': 'ok', 'added': len(added), 'group': gname})


@flask_app.route('/api/photo-groups/merge', methods=['POST'])
def api_photo_groups_merge():
    """Merges source group into target.
    Collects canonical asset_ids from both photo_groups.json AND ms_library.json,
    writes them all into target in photo_groups.json, removes source.
    Body: {source: str, target: str}
    """
    data = request.get_json(force=True, silent=True) or {}
    source, target = data.get('source', '').strip(), data.get('target', '').strip()
    if not source or not target:
        return jsonify({'status': 'error', 'msg': 'missing source or target'}), 400
    if source == target:
        return jsonify({'status': 'error', 'msg': 'source == target'}), 400

    # Collect all canonical asset_ids for the source group
    src_ids: list = []

    # 1. From ms_library — pick one canonical id per photo (priority: adobestock > shutterstock > istock > esp > first)
    _priority = ['adobestock', 'shutterstock', 'istock', 'esp']
    for photo in load_ms_library():
        if (photo.get('group') or '').strip() != source:
            continue
        stockids = {k: str(v) for k, v in photo.get('stockids', {}).items() if k in _RELEVANT_STOCKS and v}
        canonical = next((stockids[k] for k in _priority if k in stockids), next(iter(stockids.values()), None))
        if canonical and canonical not in src_ids:
            src_ids.append(canonical)

    # 2. From photo_groups.json (user-added extras not in ms_library)
    groups = load_groups()
    for aid in groups.get(source, []):
        if str(aid) not in src_ids:
            src_ids.append(str(aid))

    # Merge into target (dedup)
    tgt_ids = [str(a) for a in groups.get(target, [])]
    merged = list(tgt_ids) + [a for a in src_ids if a not in tgt_ids]
    groups[target] = merged
    if source in groups:
        del groups[source]
    save_groups(groups)

    # Also update ms_library.json: remap group field from source → target
    lib = load_ms_library()
    changed = False
    for photo in lib:
        if (photo.get('group') or '').strip() == source:
            photo['group'] = target
            changed = True
    if changed:
        save_ms_library(lib)

    return jsonify({'status': 'ok', 'merged': len(merged)})

@flask_app.route('/api/ms-library/remove-from-group', methods=['POST'])
def api_ms_remove_from_group():
    """Видаляє фото з ms_library групи: {filename}."""
    data = request.get_json(force=True, silent=True) or {}
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'status': 'error', 'msg': 'missing filename'}), 400
    photos = load_ms_library()
    for ph in photos:
        if ph.get('filename') == filename:
            ph['group'] = ''
            break
    save_ms_library(photos)
    return jsonify({'status': 'ok'})

# Cookie helpers → cookies.py

@flask_app.route('/api/import-chrome-cookies', methods=['POST'])
def api_import_chrome_cookies():
    """Imports cookies from user's native browser → all stock profiles.
    Auto-detects source: prefers 'source' in body, else tries Safari (most users)
    then Chrome. Source browser must be CLOSED.
    Body: { "source": "safari"|"chrome", "stocks": ["Adobe Stock", ...] }."""
    data = request.get_json(force=True, silent=True) or {}
    requested = (data.get('source') or '').lower()
    stocks = data.get('stocks') or list(_STOCK_COOKIE_DOMAINS.keys())

    # Windows: Chrome 127+ App-Bound Encryption (v20) makes the user's native
    # Chrome cookies undecryptable outside Chrome, and copying the encrypted
    # blobs into another profile fails (key is path-bound). Instead, log in
    # directly in the app's own Chrome profile (variant 3).
    if IS_WIN:
        results = _windows_browser_login(stocks)
        total = sum(r.get('imported', 0) for r in results)
        return jsonify({'status': 'ok', 'source': 'app-browser-login',
                        'total_imported': total, 'per_stock': results})

    # OS-aware path resolution. Safari exists ONLY on macOS.
    safari_cookies_path = (
        os.path.expanduser("~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies")
        if IS_MAC else ''
    )
    chrome_cookies_path = _chrome_cookies_path()

    # Decide which source to use
    if requested in ('safari', 'chrome'):
        source = requested
        if source == 'safari' and not IS_MAC:
            return jsonify({'status': 'error',
                            'msg': 'Safari is only available on macOS. Use Chrome on Windows.'}), 400
    else:
        # Auto-detect
        if IS_WIN:
            # Windows has no Safari — always try Chrome
            source = 'chrome' if chrome_cookies_path else 'none'
        else:
            detected = _detect_default_browser()
            if detected == 'safari' and os.path.exists(safari_cookies_path):
                source = 'safari'
            elif detected == 'chrome' and os.path.exists(chrome_cookies_path):
                source = 'chrome'
            elif detected in (None, 'edge', 'firefox', 'arc') and os.path.exists(safari_cookies_path):
                source = 'safari'
            elif os.path.exists(chrome_cookies_path):
                source = 'chrome'
            else:
                return jsonify({'status': 'error',
                                'msg': f'Default browser ({detected or "unknown"}) not supported. Only Safari/Chrome.'}), 404
        if source == 'none':
            return jsonify({'status': 'error',
                            'msg': 'Chrome cookies not found. Install Chrome and log in to a stock site first.'}), 404

    if source == 'safari':
        if _is_safari_running():
            return jsonify({'status': 'error',
                            'msg': 'Safari запущений — закрий повністю (Cmd+Q) і спробуй знову'}), 409
        if not os.path.exists(safari_cookies_path):
            return jsonify({'status': 'error',
                            'msg': f'Safari cookies not found at {safari_cookies_path}'}), 404
        try:
            all_cookies = _parse_safari_binarycookies(safari_cookies_path)
        except PermissionError:
            return jsonify({'status': 'error',
                'msg': 'macOS блокує доступ до Safari cookies. '
                       'Надай Full Disk Access застосунку:\n'
                       '1. System Settings → Privacy & Security → Full Disk Access\n'
                       '2. Натисни "+" і додай /Applications/Stock Automation.app\n'
                       '3. Перезапусти Stock Automation і спробуй знову'}), 403
        except Exception as e:
            # Errno 1 = EPERM = Operation not permitted (TCC block)
            if 'Operation not permitted' in str(e) or 'Errno 1' in str(e):
                return jsonify({'status': 'error',
                    'msg': 'macOS блокує доступ до Safari cookies. '
                           'System Settings → Privacy & Security → Full Disk Access → '
                           'додай Stock Automation.app → перезапусти застосунок.'}), 403
            return jsonify({'status': 'error', 'msg': f'Safari parse: {e}'}), 500

        # Per-stock filter + inject via Playwright
        results = []
        for stock in stocks:
            patterns = _STOCK_COOKIE_DOMAINS.get(stock, [])
            filt = [c for c in all_cookies
                    if any(p in c.get('domain', '') for p in patterns)]
            n = _inject_cookies_via_playwright(stock, filt)
            results.append({'stock': stock, 'imported': n,
                           'msg': f'matched {len(filt)} from Safari'})
        total = sum(r.get('imported', 0) for r in results)
        return jsonify({'status': 'ok', 'source': 'safari',
                        'total_imported': total, 'per_stock': results})

    # source == 'chrome'
    if _is_chrome_running():
        return jsonify({'status': 'error',
                        'msg': 'Google Chrome запущений — закрий повністю (Cmd+Q) і спробуй знову'}), 409
    results = []
    for s in stocks:
        results.append(_import_cookies_for_stock(s, chrome_cookies_path))
    total = sum(r.get('imported', 0) for r in results)
    return jsonify({'status': 'ok', 'source': 'chrome',
                    'total_imported': total, 'per_stock': results})


@flask_app.route('/api/full-reset', methods=['POST'])
def api_full_reset():
    """FULL wipe — like fresh install. Deletes:
       - sales + asset_meta tables
       - ALL files in recipes/ (incl. ms_library, photo_groups, stock_colors)
       - ALL chrome_profile* dirs (logins!)
       - ALL image caches
    Nothing kept. Body: { "confirm": "RESET" }."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get('confirm') != 'RESET':
        return jsonify({'status': 'error', 'msg': 'send {"confirm":"RESET"} to proceed'}), 400

    _sync_stop_flag[0] = True
    _sync_state['running'] = False

    # 1. Truncate DB
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.execute("DELETE FROM sales")
            c.execute("DELETE FROM asset_meta")
            c.commit()
            c.execute("VACUUM")
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'db: {e}'}), 500

    import shutil as _sh

    # 2. Wipe recipes/ entirely
    if os.path.isdir(RECIPES_DIR):
        for entry in os.listdir(RECIPES_DIR):
            fp = os.path.join(RECIPES_DIR, entry)
            try:
                if os.path.isfile(fp): os.remove(fp)
                elif os.path.isdir(fp): _sh.rmtree(fp)
            except Exception: pass

    # 3. Wipe all chrome_profile* and *_profile dirs (LOGINS GONE)
    profiles_removed = 0
    for entry in os.listdir(_BASE_DIR):
        if (entry.startswith('chrome_profile') or entry.endswith('_profile')
                or entry.startswith('getty_profile')):
            fp = os.path.join(_BASE_DIR, entry)
            if os.path.isdir(fp):
                try:
                    _sh.rmtree(fp)
                    profiles_removed += 1
                except Exception: pass

    # 4. Wipe image caches
    cache_removed = 0
    for cache_dir in (CACHE_DIR, MATCH_CACHE_DIR, MS_CACHE_DIR, ICON_CACHE_DIR):
        if os.path.isdir(cache_dir):
            for f in os.listdir(cache_dir):
                fp = os.path.join(cache_dir, f)
                if os.path.isfile(fp):
                    try: os.remove(fp); cache_removed += 1
                    except Exception: pass

    return jsonify({'status': 'ok',
                    'profiles_wiped': profiles_removed,
                    'cache_files_wiped': cache_removed})


@flask_app.route('/api/rebuild-from-db', methods=['POST'])
def api_rebuild_from_db():
    """Rebuild groups & matches from EXISTING DB data — no re-sync from APIs.
    Keeps: sales, asset_meta, ms_library, chrome_profile*, stock_colors.
    Wipes: photo_groups, _cross_stock_matches, _manual_overrides.
    Then runs rebuild-matches synchronously.
    Body: { "confirm": "REBUILD" }."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get('confirm') != 'REBUILD':
        return jsonify({'status': 'error', 'msg': 'send {"confirm":"REBUILD"} to proceed'}), 400

    for fname in ('photo_groups.json', '_manual_overrides.json',
                  '_cross_stock_matches.json'):
        fp = os.path.join(RECIPES_DIR, fname)
        if os.path.exists(fp):
            try: os.remove(fp)
            except Exception: pass

    try:
        resp = api_rebuild_matches()
        return jsonify({'status': 'ok', 'result': resp.get_json()})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@flask_app.route('/api/rebuild-groups', methods=['POST'])
def api_rebuild_groups():
    """Rebuild group structure from scratch using existing sales data.

    Wipes: ms_library.json, photo_groups.json, _manual_overrides.json,
           _cross_stock_matches.json
    Keeps: sales.db, asset_meta (pHash + RGB), chrome_profile*, stock_colors

    Then: triggers a MS+ collector run (refreshes ms_library) in background.
    rebuild-matches will be auto-invoked at the end via _sync_all_global path
    (but only MS+ runs here, not all stocks). The user can trigger /api/rebuild-
    matches manually after MS+ finishes, OR we run it here at the end.

    Body: { "confirm": "REBUILD" }."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get('confirm') != 'REBUILD':
        return jsonify({'status': 'error', 'msg': 'send {"confirm":"REBUILD"} to proceed'}), 400

    # 1. Wipe group/match data files + state cursors so MS+ does full re-fetch
    # and rebuild-matches doesn't early-exit on stale fingerprints.
    for fname in ('photo_groups.json', '_manual_overrides.json',
                  '_cross_stock_matches.json', 'ms_library.json',
                  '_ms_dirs_state.json', '_matches_state.json',
                  '_ms_group_snapshot.json'):
        fp = os.path.join(RECIPES_DIR, fname)
        if os.path.exists(fp):
            try: os.remove(fp)
            except Exception: pass

    # 1b. Wipe MS+ thumbnail cache + ms_meta so fresh sync re-downloads & re-hashes
    if os.path.isdir(MS_CACHE_DIR):
        for f in os.listdir(MS_CACHE_DIR):
            try: os.remove(os.path.join(MS_CACHE_DIR, f))
            except Exception: pass
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as _c:
            _c.execute("DELETE FROM ms_meta")
            _c.commit()
    except Exception: pass

    # 2. Trigger MS+ sync in background (re-fetches ms_library, then rebuild-matches)
    def _run():
        try:
            ms_url = STOCK_URLS.get("Microstock+", "https://microstock.plus/myfiles")
            _sync_state["running"] = True
            _sync_state["log"] = []
            _sync_stop_flag[0] = False
            _collect_one_stock_global("Microstock+", ms_url)
            # Auto-trigger rebuild-matches now that ms_library is fresh
            try:
                with flask_app.test_request_context():
                    resp = api_rebuild_matches()
                _sync_log(f"✅ Groups rebuilt: {resp.get_json()}")
            except Exception as ex:
                _sync_log(f"⚠️ rebuild-matches failed: {ex}")
        finally:
            _sync_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'ok', 'msg': 'MS+ sync + rebuild started in background'})


@flask_app.route('/api/reset-db', methods=['POST'])
def api_reset_db():
    """Full data wipe — app returns to fresh state. KEEPS ONLY chrome_profile*
    (logins) and stock_colors.json (UI preference). Everything else (sales,
    asset_meta, ms_library, photo_groups, matches, overrides, image caches,
    processed_dates) is removed. Next sync rebuilds everything from scratch.
    Body: { "confirm": "RESET" }."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get('confirm') != 'RESET':
        return jsonify({'status': 'error', 'msg': 'send {"confirm":"RESET"} to proceed'}), 400

    _sync_stop_flag[0] = True
    _sync_state['running'] = False

    removed_counts = {}

    # 1. Truncate DB tables
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.execute("DELETE FROM sales")
            c.execute("DELETE FROM asset_meta")
            c.commit()
            c.execute("VACUUM")
        removed_counts['db_tables'] = 'cleared'
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'db: {e}'}), 500

    # 2. Remove all recipes files EXCEPT stock_colors.json
    try:
        for entry in os.listdir(RECIPES_DIR):
            if entry == 'stock_colors.json':
                continue
            fp = os.path.join(RECIPES_DIR, entry)
            if os.path.isfile(fp):
                os.remove(fp)
        removed_counts['recipes'] = 'cleared'
    except Exception:
        removed_counts['recipes'] = 'partial'

    # 3. Remove ms_library.json from base dir if it lives there too
    for legacy in (os.path.join(_BASE_DIR, 'ms_library.json'),):
        if os.path.exists(legacy):
            try: os.remove(legacy)
            except Exception: pass

    # 4. Wipe image caches (sync will rebuild them as it downloads thumbnails)
    import shutil as _sh
    cache_removed = 0
    for cache_dir in (CACHE_DIR, MATCH_CACHE_DIR, MS_CACHE_DIR, ICON_CACHE_DIR):
        if os.path.isdir(cache_dir):
            try:
                files = [f for f in os.listdir(cache_dir) if not f.startswith('.')]
                for f in files:
                    fp = os.path.join(cache_dir, f)
                    if os.path.isfile(fp):
                        os.remove(fp)
                cache_removed += len(files)
            except Exception:
                pass
    removed_counts['cache_files'] = cache_removed

    return jsonify({'status': 'ok',
                    'wiped': removed_counts,
                    'kept': ['chrome_profile*', 'stock_colors.json']})


@flask_app.route('/api/reset', methods=['POST'])
def api_reset():
    """Full account reset: wipes sales DB, all browser profiles, image caches,
    and processed-dates log. Keeps user groups, stock colors, and ms_library.
    Body: { "confirm": "RESET" }  — required to prevent accidental calls.
    """
    data = request.get_json(force=True, silent=True) or {}
    if data.get('confirm') != 'RESET':
        return jsonify({'status': 'error', 'msg': 'send {"confirm":"RESET"} to proceed'}), 400

    # Stop any active sync first
    global _sync_state
    _sync_state['running'] = False

    removed = []
    errors  = []

    # 1. Drop + recreate sales.db
    try:
        db_path = os.path.join(_BASE_DIR, 'sales.db')
        if os.path.exists(db_path):
            os.remove(db_path)
        init_db()
        removed.append('sales.db')
    except Exception as e:
        errors.append(f'sales.db: {e}')

    # 2. Delete all browser profile directories
    import glob
    profile_patterns = [
        os.path.join(_BASE_DIR, 'chrome_profile*'),
        os.path.join(_BASE_DIR, '*_profile'),
        os.path.join(_BASE_DIR, '*_profile_*'),
    ]
    for pat in profile_patterns:
        for d in glob.glob(pat):
            if os.path.isdir(d):
                try:
                    import shutil
                    shutil.rmtree(d)
                    removed.append(os.path.basename(d))
                except Exception as e:
                    errors.append(f'{os.path.basename(d)}: {e}')

    # 3. Delete image caches
    cache_dirs = [
        os.path.join(_BASE_DIR, 'img_cache'),
        os.path.join(_BASE_DIR, 'img_cache_match'),
        os.path.join(_BASE_DIR, 'img_cache_ms'),
        os.path.join(_BASE_DIR, 'img_cache_icons'),
    ]
    for d in cache_dirs:
        if os.path.isdir(d):
            try:
                import shutil
                shutil.rmtree(d)
                os.makedirs(d)          # recreate empty dir
                removed.append(os.path.basename(d))
            except Exception as e:
                errors.append(f'{os.path.basename(d)}: {e}')

    # 4. Clear all recipes state (processed dates, groups, matches)
    recipes_to_clear = [
        '_processed_dates.json',
        'photo_groups.json',
        '_cross_stock_matches.json',
    ]
    for fname in recipes_to_clear:
        fpath = os.path.join(RECIPES_DIR, fname)
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
                removed.append(fname)
            except Exception as e:
                errors.append(f'{fname}: {e}')

    _app_log(f'[reset] removed: {removed}, errors: {errors}')
    return jsonify({'status': 'ok', 'removed': removed, 'errors': errors})


@flask_app.route('/api/deduplicate', methods=['POST'])
def api_deduplicate():
    """One-shot cleanup:
    1. Remove duplicate sale rows (keep highest-price row per stock+asset_id+date-day).
    2. Deduplicate IDs within each group.
    3. Remove IDs that appear in multiple groups (keep in the first group alphabetically).
    """
    result = {}

    # ── 1. Sales DB dedup ────────────────────────────────────────────────────
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            before = c.execute('SELECT COUNT(*) FROM sales').fetchone()[0]
            # Keep the row with the highest price for each (stock, asset_id, date-day)
            c.execute('''
                DELETE FROM sales WHERE rowid NOT IN (
                    SELECT MAX(rowid) FROM sales
                    GROUP BY stock, asset_id, DATE(date)
                )
            ''')
            c.commit()
            after = c.execute('SELECT COUNT(*) FROM sales').fetchone()[0]
        result['sales_removed'] = before - after
        result['sales_remaining'] = after
    except Exception as e:
        result['sales_error'] = str(e)

    # ── 2. Groups dedup ──────────────────────────────────────────────────────
    try:
        groups = load_groups()
        # Pass 1: deduplicate within each group, sort groups alphabetically
        clean = {}
        for name in sorted(groups.keys()):
            clean[name] = list(dict.fromkeys(str(a) for a in groups[name]))

        # Pass 2: if an ID appears in multiple groups, keep it only in the
        # first group alphabetically (the dict is already sorted above)
        seen_ids: set = set()
        cross_removed = 0
        final = {}
        for name, ids in clean.items():
            deduped = [a for a in ids if a not in seen_ids]
            cross_removed += len(ids) - len(deduped)
            seen_ids.update(deduped)
            if deduped:
                final[name] = deduped

        save_groups(final)
        result['groups_cross_removed'] = cross_removed
        result['groups_total'] = len(final)
    except Exception as e:
        result['groups_error'] = str(e)

    _app_log(f'[deduplicate] {result}')
    return jsonify({'status': 'ok', **result})


@flask_app.route('/api/group-names', methods=['GET'])
def api_group_names():
    """Returns sorted list of all group names (ms_library + user)."""
    lib = load_ms_library()
    ms_names = {p.get('group', '').strip() for p in lib if p.get('group', '').strip()}
    user_names = set(load_groups().keys())
    return jsonify(sorted(ms_names | user_names))


# _hash_based_matches → matching_engine.py

@flask_app.route('/api/refresh-istock-thumbs', methods=['POST'])
def api_refresh_istock_thumbs():
    """Re-fetch ThumbnailUrl from ESP API for iStock asset_ids missing pHash.
    Getty signed URLs expire — sales records may have stale/empty thumb_url and
    no cached file in img_cache/. This walks the ESP downloads_for_search API
    monthly windows, finds matches against our missing-pHash set, downloads
    fresh thumbs into img_cache/ (which also computes pHash via _save_asset_meta),
    then triggers rebuild-matches.

    Body: {} — no params. Reads Safari ccw cookie for auth.
    """
    import urllib.parse as _up
    import base64

    # Build target set: iStock aids in sales without pHash
    with sqlite3.connect(DB_NAME, timeout=15) as c:
        all_istock = set(r[0] for r in c.execute(
            "SELECT DISTINCT asset_id FROM sales "
            "WHERE stock IN ('iStock','iStockphoto') AND asset_id IS NOT NULL"
        ) if r[0])
        hashed = set(r[0] for r in c.execute(
            "SELECT asset_id FROM asset_meta WHERE stock='iStock' "
            "AND thumb_hash IS NOT NULL AND thumb_hash != ''"
        ) if r[0])
    target = all_istock - hashed
    if not target:
        return jsonify({'status': 'ok', 'msg': 'no iStock aids missing pHash', 'updated': 0})

    try:
        all_cookies = _load_browser_cookies()
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'cookie read failed: {e}'}), 500
    if not all_cookies:
        return jsonify({'status': 'error',
                        'msg': 'no browser cookies (Safari on mac / Chrome on win)'}), 400

    g_cookies = {c['name']: c['value'] for c in all_cookies
                 if 'gettyimages' in c.get('domain', '').lower()}
    if 'ccw' not in g_cookies:
        return jsonify({'status': 'error', 'msg': 'no ccw cookie — login to Getty in Safari'}), 401

    try:
        ccw_raw = g_cookies['ccw']
        b64 = _up.unquote(ccw_raw).split("|")[0]
        b64 += "=" * (4 - len(b64) % 4)
        sts_token = json.loads(base64.b64decode(b64))["sts_token"]
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'sts_token decode: {e}'}), 500

    def _run():
        _sync_state["running"] = True
        _sync_state["log"] = []
        _sync_stop_flag[0] = False
        _sync_log(f"🚀 iStock thumb refresh: {len(target)} aids потребують pHash")

        esp_base = "https://esp.gettyimages.com"
        esp_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {sts_token}",
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                           "Version/17.5 Safari/605.1.15"),
        }
        import calendar as _cal
        from datetime import datetime as _dt
        now = _dt.now()
        remaining = set(target)
        updated = 0
        try:
            for offset_m in range(60):  # up to 5 years
                if _sync_stop_flag[0] or not remaining:
                    break
                m_i = now.month - offset_m
                y_i = now.year
                while m_i <= 0:
                    m_i += 12; y_i -= 1
                from_date = f"{y_i}-{m_i:02d}-01"
                last_d = _cal.monthrange(y_i, m_i)[1]
                to_date = f"{y_i}-{m_i:02d}-{last_d:02d}"
                if y_i == now.year and m_i == now.month:
                    to_date = now.strftime("%Y-%m-%d")
                page_n = 1
                while True:
                    if _sync_stop_flag[0] or not remaining: break
                    url = (f"{esp_base}/api/account/v1/statistics/downloads_for_search"
                           f"?orderResultsBy=LastDownloadDate&sortDirection=Descending"
                           f"&page={page_n}&pageSize=50"
                           f"&fromDate={from_date}&toDate={to_date}"
                           f"&primaryDatePeriod=by_month")
                    try:
                        r = req_lib.get(url, headers=esp_headers, cookies=g_cookies, timeout=20)
                        if r.status_code != 200:
                            _sync_log(f"  ⚠️ ESP {y_i}-{m_i:02d} p{page_n}: HTTP {r.status_code}")
                            break
                        data = r.json()
                    except Exception as ex:
                        _sync_log(f"  ⚠️ ESP {y_i}-{m_i:02d} p{page_n}: {ex}")
                        break
                    items = data.get("AssetDownloadSummaries", []) or []
                    if not items: break
                    for item in items:
                        aid = str(item.get("MasterId", ""))
                        thumb = item.get("ThumbnailUrl", "")
                        if not aid or not thumb or aid not in remaining:
                            continue
                        # Remove cached file first if it exists with no hash (rare)
                        cached = os.path.join(CACHE_DIR, f"{aid}.jpg")
                        if os.path.exists(cached):
                            try: os.remove(cached)
                            except Exception: pass
                        path = load_img(aid, thumb, stock="iStock")
                        if path:
                            updated += 1
                            remaining.discard(aid)
                    total_pages = (data.get("TotalAssetCount", 0) + 49) // 50
                    if page_n >= total_pages: break
                    page_n += 1
                    time.sleep(0.1)
                _sync_log(f"  📦 {y_i}-{m_i:02d}: оновлено {updated}, лишилось {len(remaining)}")
            _sync_log(f"✅ iStock thumbs: оновлено {updated} з {len(target)} (лишилось {len(remaining)})")

            # Trigger rebuild-matches to use new pHashes
            try:
                with flask_app.test_request_context():
                    resp = api_rebuild_matches()
                _sync_log(f"✅ rebuild-matches: {resp.get_json()}")
            except Exception as ex:
                _sync_log(f"⚠️ rebuild-matches: {ex}")
        finally:
            _sync_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'ok', 'msg': f'iStock refresh started for {len(target)} aids',
                    'targeted': len(target)})


@flask_app.route('/api/compute-hashes', methods=['POST'])
def api_compute_hashes():
    """
    Backfill thumb_hash + dominant RGB for every cached thumbnail in img_cache/.
    Re-computes rows where RGB is missing (NULL r), keeps already-complete rows.
    """
    if not os.path.isdir(CACHE_DIR):
        return jsonify({'computed': 0, 'msg': 'no cache dir'})

    # Build aid -> stock map from sales
    aid_stock = {}
    with sqlite3.connect(DB_NAME, timeout=15) as c:
        for aid, stock in c.execute("SELECT DISTINCT asset_id, stock FROM sales WHERE asset_id IS NOT NULL"):
            if aid and stock and aid not in aid_stock:
                aid_stock[str(aid)] = stock

        # Already-complete rows (have RGB) — skip these
        complete = set(
            (s, a) for s, a in c.execute(
                "SELECT stock, asset_id FROM asset_meta WHERE r IS NOT NULL"
            )
        )

    computed = 0
    skipped = 0
    rows = []
    for fname in os.listdir(CACHE_DIR):
        if not fname.lower().endswith('.jpg'):
            continue
        aid = fname[:-4]
        stock = aid_stock.get(aid)
        if not stock:
            skipped += 1
            continue
        if (stock, aid) in complete:
            continue
        h, ar, r, g, b = _dhash_from_path(os.path.join(CACHE_DIR, fname))
        if not h:
            continue
        rows.append((stock, aid, h, ar, r, g, b, datetime.now().isoformat()))
        computed += 1

    if rows:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.executemany(
                "INSERT OR REPLACE INTO asset_meta "
                "(stock, asset_id, thumb_hash, aspect_ratio, r, g, b, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows
            )
            c.commit()
    return jsonify({'computed': computed, 'skipped_no_stock': skipped,
                    'total_complete': len(complete) + computed})


@flask_app.route('/api/compute-ms-hashes', methods=['POST'])
def api_compute_ms_hashes():
    """
    Backfill thumb_hash + dominant RGB for every reference thumbnail in img_cache_ms/.
    Skips files already hashed (present in ms_meta with non-NULL r).
    """
    if not os.path.isdir(MS_CACHE_DIR):
        return jsonify({'computed': 0, 'msg': 'no ms cache dir'})

    with sqlite3.connect(DB_NAME, timeout=15) as c:
        complete = set(r[0] for r in c.execute("SELECT fname FROM ms_meta WHERE r IS NOT NULL"))

    computed = 0
    rows = []
    for fname in os.listdir(MS_CACHE_DIR):
        if not fname.lower().endswith('.jpg'):
            continue
        base = fname[:-4]
        if base in complete:
            continue
        h, ar, r, g, b = _dhash_from_path(os.path.join(MS_CACHE_DIR, fname))
        if not h:
            continue
        rows.append((base, h, ar, r, g, b, datetime.now().isoformat()))
        computed += 1
        # flush in batches to keep memory bounded
        if len(rows) >= 1000:
            with sqlite3.connect(DB_NAME, timeout=15) as c:
                c.executemany(
                    "INSERT OR REPLACE INTO ms_meta "
                    "(fname, thumb_hash, aspect_ratio, r, g, b, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)", rows)
                c.commit()
            rows = []

    if rows:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.executemany(
                "INSERT OR REPLACE INTO ms_meta "
                "(fname, thumb_hash, aspect_ratio, r, g, b, updated_at) "
                "VALUES (?,?,?,?,?,?,?)", rows)
            c.commit()
    return jsonify({'computed': computed,
                    'total_complete': len(complete) + computed})


# _ms_fname_to_libentry, _ms_visual_matches, _filename_fallback_matches, _apply_manual_overrides → matching_engine.py

@flask_app.route('/api/rebuild-matches', methods=['POST'])
def api_rebuild_matches():
    """Rebuild cross-stock matches + sync ms_library groups into photo_groups.json.

    Pass order:
      A. pHash+RGB clustering — INCREMENTAL (skip OLD×OLD pairs)
      B. ms_library stockids — SKIP when ms_library unchanged
      C. Filename fallback (cheap, always)
      D. Sync MS+ groups → photo_groups — DIFF-BASED (snapshot)
      E. Auto-propagate via clusters (cheap, always)
      F. MS+ visual matching — INCREMENTAL (cursors both sides)
      G. Dedup
      H. Apply manual overrides

    Early-exit: if asset_meta + ms_meta rowids AND ms_library fingerprint all
    unchanged since last rebuild, return cached counts without running passes.
    """
    import hashlib
    lib = load_ms_library()
    overrides = _load_overrides()
    _PRIMARY_PRIORITY = ['adobestock', 'shutterstock', 'istock', 'esp', 'depositphotos']

    def _pick_primary(stockids):
        for key in _PRIMARY_PRIORITY:
            v = stockids.get(key)
            if v: return str(v)
        return None

    # ── State + change detection ─────────────────────────────────────────
    _state_file    = os.path.join(RECIPES_DIR, '_matches_state.json')
    _snapshot_file = os.path.join(RECIPES_DIR, '_ms_group_snapshot.json')
    try:
        with open(_state_file) as _f: _state = json.load(_f)
    except Exception:
        _state = {}
    _last_am_rowid = _state.get('last_asset_meta_rowid')
    _last_mm_rowid = _state.get('last_ms_meta_rowid')
    _last_lib_fp   = _state.get('last_ms_lib_fingerprint')

    with sqlite3.connect(DB_NAME, timeout=15) as _c:
        _r = _c.execute("SELECT MAX(rowid) FROM asset_meta WHERE thumb_hash IS NOT NULL AND thumb_hash != ''").fetchone()
        _cur_am_rowid = _r[0] or 0
        _r = _c.execute("SELECT MAX(rowid) FROM ms_meta WHERE thumb_hash IS NOT NULL AND thumb_hash != ''").fetchone()
        _cur_mm_rowid = _r[0] or 0

    _h = hashlib.sha1()
    for p in sorted(lib, key=lambda x: x.get('basepath', '')):
        _h.update(repr((p.get('basepath', ''), p.get('group', ''),
                        tuple(sorted((p.get('stockids') or {}).items())))).encode())
    _cur_lib_fp = _h.hexdigest()

    # Guard against stale/foreign state. _matches_state.json can be carried over
    # from another machine (e.g. moving the data dir Mac→Windows), where
    # asset_meta rowids belong to a different table. A saved cursor beyond the
    # current max can't be trusted → drop it so a full rebuild runs instead of
    # a false "nothing changed" cache hit.
    if _last_am_rowid is not None and _last_am_rowid > _cur_am_rowid:
        _last_am_rowid = None
    if _last_mm_rowid is not None and _last_mm_rowid > _cur_mm_rowid:
        _last_mm_rowid = None

    am_unchanged  = (_last_am_rowid == _cur_am_rowid)
    mm_unchanged  = (_last_mm_rowid == _cur_mm_rowid)
    lib_unchanged = (_last_lib_fp == _cur_lib_fp)

    # ── Early exit ───────────────────────────────────────────────────────
    matches_path = os.path.join(RECIPES_DIR, '_cross_stock_matches.json')
    if (am_unchanged and mm_unchanged and lib_unchanged
            and _last_am_rowid is not None and os.path.exists(matches_path)):
        cur_matches = _load_matches()
        cur_groups  = load_groups()
        return jsonify({'status': 'ok', 'cached': True,
                        'entries': len(cur_matches),
                        'photos': 0, 'groups': len(cur_groups),
                        'hash_pairs': 0, 'ms_lib_pairs': 0,
                        'filename_pairs': 0, 'auto_grouped': 0,
                        'ms_visual_grouped': 0, 'override_changes': 0})

    # ── Pass A: pHash+RGB clustering (incremental) ───────────────────────
    if _last_am_rowid is None:
        matches, hash_pairs, _new_am_rowid = _hash_based_matches({}, incremental_from_rowid=None)
    else:
        matches, hash_pairs, _new_am_rowid = _hash_based_matches(
            _load_matches(), incremental_from_rowid=_last_am_rowid)

    # ── Pass B: ms_library stockids — skip when lib unchanged ────────────
    aid_to_key = {m: k for k, members in matches.items() for m in members}
    ms_added_pairs = 0
    if not lib_unchanged or _last_lib_fp is None:
        for photo in lib:
            stockids = photo.get('stockids', {})
            ids = [str(v) for k, v in stockids.items() if k in _RELEVANT_STOCKS and v]
            if len(ids) < 2: continue
            existing_keys = {aid_to_key[i] for i in ids if i in aid_to_key}
            if existing_keys:
                target = sorted(existing_keys)[0]
                combined = set(matches.get(target, []))
                for k in existing_keys - {target}:
                    combined.update(matches.pop(k, []))
                combined.update(ids)
                matches[target] = sorted(combined)
                for m in matches[target]:
                    aid_to_key[m] = target
            else:
                primary = _pick_primary(stockids) or ids[0]
                matches[primary] = sorted(set(ids))
                for m in ids:
                    aid_to_key[m] = primary
                ms_added_pairs += len(ids) - 1

    # ── Pass C: filename fallback ────────────────────────────────────────
    matches, fn_pairs = _filename_fallback_matches(matches, lib)
    _save_matches(matches)

    # ── Pass D: sync MS+ groups → photo_groups (DIFF-BASED) ──────────────
    # Build current MS+ authoritative {primary → group}.
    groups = load_groups()
    ms_authoritative = {}
    for photo in lib:
        gname = (photo.get('group') or '').strip()
        if not gname: continue
        stockids = photo.get('stockids') or {}
        primary = _pick_primary(stockids)
        if not primary: continue
        ms_authoritative[primary] = gname

    # Load previous snapshot. First-time run: full processing.
    try:
        with open(_snapshot_file) as _f: _snapshot = json.load(_f)
    except Exception:
        _snapshot = {}

    aid_to_key_purge = {m: k for k, members in matches.items() for m in members}
    photos_synced = 0

    if not _snapshot:
        # ⚠️ First run after migration / fresh DB MUST use classic full Pass D.
        # Reason: existing groups may already contain primaries (added before
        # snapshot existed). Diff path treats all primaries as "added" and would
        # place them in MS+ groups WITHOUT purging from old groups → photos end
        # up in two groups. Classic purge-then-reassign is the only safe init.
        # Subsequent runs use diff-based path below (preserves user moves).
        purge_aids = set()
        for primary in ms_authoritative:
            purge_aids.add(primary)
            ck = aid_to_key_purge.get(primary)
            if ck:
                purge_aids.update(matches.get(ck, []))
        if purge_aids:
            for gname in list(groups.keys()):
                groups[gname] = [a for a in groups[gname] if a not in purge_aids]
        for primary, gname in ms_authoritative.items():
            if gname not in groups: groups[gname] = []
            if primary not in groups[gname]:
                groups[gname].append(primary)
                photos_synced += 1
    else:
        # Diff against last snapshot: only touch primaries whose MS+ assignment
        # actually changed. Primaries that MS+ didn't move stay wherever the
        # user put them — this is how user UI moves survive across rebuilds.
        diff_added = {p: g for p, g in ms_authoritative.items() if p not in _snapshot}
        diff_moved = {p: g for p, g in ms_authoritative.items()
                      if p in _snapshot and _snapshot[p] != g}

        # Apply moved: purge primary+siblings from OLD MS+ group only, add to NEW.
        for primary, new_gname in diff_moved.items():
            old_gname = _snapshot.get(primary, '')
            purge_set = {primary}
            ck = aid_to_key_purge.get(primary)
            if ck:
                purge_set.update(matches.get(ck, []))
            if old_gname in groups:
                groups[old_gname] = [a for a in groups[old_gname] if a not in purge_set]
            if new_gname not in groups:
                groups[new_gname] = []
            if primary not in groups[new_gname]:
                groups[new_gname].append(primary)
                photos_synced += 1

        # Apply added: new primary appears for the first time — place in MS+ group.
        for primary, gname in diff_added.items():
            if gname not in groups:
                groups[gname] = []
            if primary not in groups[gname]:
                groups[gname].append(primary)
                photos_synced += 1

    # ── Pass E: auto-propagate via match clusters ────────────────────────
    aid_to_group = {}
    for gname, aids in groups.items():
        for aid in aids:
            aid_to_group[aid] = gname

    aid_to_matchkey = {}
    for key, members in matches.items():
        for m in members:
            aid_to_matchkey[m] = key

    auto_added = 0
    for gname in list(groups.keys()):
        for aid in list(groups[gname]):
            mkey = aid_to_matchkey.get(aid)
            if not mkey: continue
            for sib in matches.get(mkey, []):
                if sib == aid or sib in aid_to_group: continue
                groups[gname].append(sib)
                aid_to_group[sib] = gname
                auto_added += 1

    # ── Pass F: MS+ visual matching (incremental) ────────────────────────
    ms_added = 0
    if am_unchanged and mm_unchanged and _last_am_rowid is not None:
        ms_additions = {}
    else:
        ms_additions = _ms_visual_matches(
            aid_to_group,
            last_asset_meta_rowid=_last_am_rowid,
            last_ms_meta_rowid=_last_mm_rowid,
        )
    for gname, aids in ms_additions.items():
        if gname not in groups:
            groups[gname] = []
        groups[gname].extend(aids)
        ms_added += len(aids)

    # ── Pass G: dedup ────────────────────────────────────────────────────
    for gname in list(groups.keys()):
        seen: set = set()
        deduped = []
        for aid in groups[gname]:
            if aid not in seen:
                seen.add(aid)
                deduped.append(aid)
        groups[gname] = deduped
    save_groups(groups)

    # ── Pass H: apply manual overrides ───────────────────────────────────
    matches, override_changes = _apply_manual_overrides(matches, overrides)
    _save_matches(matches)

    # ── Persist state ────────────────────────────────────────────────────
    try:
        with open(_state_file, 'w') as _f:
            json.dump({
                'last_asset_meta_rowid':   _cur_am_rowid,
                'last_ms_meta_rowid':      _cur_mm_rowid,
                'last_ms_lib_fingerprint': _cur_lib_fp,
            }, _f)
    except Exception: pass
    try:
        with open(_snapshot_file, 'w') as _f:
            json.dump(ms_authoritative, _f)
    except Exception: pass

    return jsonify({'status': 'ok',
                    'entries': len(matches),
                    'photos': photos_synced,
                    'groups': len(groups),
                    'hash_pairs': hash_pairs,
                    'ms_lib_pairs': ms_added_pairs,
                    'filename_pairs': fn_pairs,
                    'auto_grouped': auto_added,
                    'ms_visual_grouped': ms_added,
                    'override_changes': override_changes})


@flask_app.route('/api/match-override', methods=['POST'])
def api_match_override():
    """User-triggered: persist a manual link/unlink that will survive future
    rebuild-matches runs. Body: {action: 'link'|'unlink', primary, asset_id}."""
    body = request.get_json(force=True, silent=True) or {}
    action  = body.get('action')
    primary = str(body.get('primary', '')).strip()
    aid     = str(body.get('asset_id', '')).strip()
    if action not in ('link', 'unlink') or not primary or not aid:
        return jsonify({'ok': False, 'msg': 'need {action: link|unlink, primary, asset_id}'}), 400
    ov = _load_overrides()
    key = 'linked' if action == 'link' else 'unlinked'
    bucket = ov.setdefault(key, {})
    lst = set(bucket.get(primary, []))
    lst.add(aid)
    bucket[primary] = sorted(lst)
    # Remove conflicting entry from the OTHER bucket (link cancels prior unlink and vice versa)
    other_key = 'unlinked' if action == 'link' else 'linked'
    other = ov.setdefault(other_key, {})
    if primary in other and aid in other[primary]:
        other[primary] = [x for x in other[primary] if x != aid]
        if not other[primary]:
            other.pop(primary)
    _save_overrides(ov)
    return jsonify({'ok': True, 'action': action, 'primary': primary, 'asset_id': aid})

@flask_app.route('/api/matches', methods=['GET'])
def api_matches_get():
    """Повертає cross-stock matches."""
    return jsonify(_load_matches())

@flask_app.route('/api/matches', methods=['POST'])
def api_matches_post():
    """Зберігає cross-stock matches."""
    data = request.get_json(force=True, silent=True) or {}
    _save_matches(data)
    return jsonify({"status": "ok"})

@flask_app.route('/api/sync/status', methods=['GET'])
def api_sync_status():
    """Поточний стан синхронізації."""
    return jsonify(_sync_state)

@flask_app.route('/api/debug/log', methods=['GET'])
def api_debug_log():
    """Last N lines of app.log + sync log + platform info. UI exposes a Copy
    Logs button (Browser tab) so the user can paste into bug reports without
    digging through filesystem paths that differ by OS."""
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

@flask_app.route('/api/sync/recent-keys', methods=['GET'])
def api_sync_recent_keys():
    """Keys of sales inserted during the most recent sync session.
    Client uses these to paint blue highlights. Survives until next sync starts."""
    with _session_new_keys_lock:
        return jsonify(list(_session_new_keys))


@flask_app.route('/api/sync/recent-items', methods=['GET'])
def api_sync_recent_items():
    """Return full sale records for all keys in _session_new_keys.
    Used by Downloads tab to place ALL new items (including old-dated Shutterstock
    incremental records) at the top of the feed, regardless of their date."""
    with _session_new_keys_lock:
        keys = list(_session_new_keys)
    if not keys:
        return jsonify([])
    # Parse keys: "asset_id|date|stock|price"
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
    # Sort by date DESC so most recent new items appear first within the new block
    result.sort(key=lambda x: x['date'], reverse=True)
    return jsonify(result)

@flask_app.route('/api/sync/stream')
def api_sync_stream():
    """SSE stream для live логів синхронізації."""
    import time as _time
    def _generate():
        last = 0
        try:
            while True:
                # snapshot under lock — _sync_log() mutates the list from collector threads
                with _sync_log_lock:
                    logs = list(_sync_state.get("log", []))
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
    from flask import Response
    return Response(_generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@flask_app.route('/api/sync/start', methods=['POST'])
def api_sync_start():
    """Запустити синхронізацію — одного стоку або всіх."""
    if _sync_all_active[0]:
        return jsonify({"ok": False, "msg": "already running"})
    data  = request.get_json(force=True, silent=True) or {}
    stock = data.get('stock', '').strip()

    def _run_single(name, u):
        # Properly manage sync state so UI sees correct running/done flow
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

@flask_app.route('/api/sync/stop', methods=['POST'])
def api_sync_stop():
    """Зупинити поточну синхронізацію."""
    _sync_stop_flag[0] = True
    return jsonify({"ok": True})

@flask_app.route('/api/sync/headless', methods=['POST'])
def api_sync_headless():
    """Перемикач headless режиму (true/false)."""
    global _headless_mode
    data = request.get_json(force=True, silent=True) or {}
    with _headless_lock:
        _headless_mode = bool(data.get('headless', True))
    return jsonify({"headless": _headless_mode})

# ═══════════════════════════════════════════════════════════
# NETWORK INSPECTOR  — для підключення нових стоків
# ═══════════════════════════════════════════════════════════
_inspector_state: dict = {"running": False, "stock": ""}
_inspector_log: list   = []
_inspector_lock  = threading.Lock()
_inspector_browser_ref: list = [None]
_inspector_log_file = [None]

INSPECTOR_LOG_DIR = os.path.join(_BASE_DIR, "inspector_logs")
os.makedirs(INSPECTOR_LOG_DIR, exist_ok=True)

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

    safe_name    = stock_name.replace(" ", "_")
    profile_dir  = os.path.abspath(f"chrome_profile_{safe_name}")
    log_path     = os.path.join(INSPECTOR_LOG_DIR, f"{safe_name}.log")

    # open log file (append so old sessions are kept, separator added)
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
            # Inspector needs a visible, normal-sized window (not off-screen like sync browser)
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

            # attach to all existing pages
            for p in browser.pages:
                _attach_page(p)
            # if no pages yet, open one at the start URL
            if not browser.pages:
                pg = browser.new_page()
                try: pg.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                except Exception: pass

            # attach to any new pages/tabs the user opens
            browser.on("page", _attach_page)

            _inspector_log_push("✅ Browser open — log in and navigate to the earnings/sales page")
            # keep alive using Playwright-native wait so the event loop can dispatch response events
            while _inspector_state["running"]:
                try:
                    pages = browser.pages
                    if not pages:
                        # all tabs closed — reopen at start URL so user can continue
                        pg = browser.new_page()
                        try: pg.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                        except Exception: pass
                    else:
                        pages[0].wait_for_timeout(500)
                except Exception:
                    break
            # close browser from inside the greenlet thread (avoids cross-thread greenlet error)
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

_INSPECTOR_URLS = {
    # Override sync URLs with pages that expose the most useful XHR endpoints
    # (insights/stats pages, where date filters live).
    "Adobe Stock":   "https://contributor.stock.adobe.com/en/insights/sales-earnings",
    "Shutterstock":  "https://submit.shutterstock.com/earnings",
    "Getty Images":  "https://esp.gettyimages.com/contribute/stats",
    "iStock":        "https://esp.gettyimages.com/contribute/stats",
    "Depositphotos": "https://depositphotos.com/account/sales-history.html",
    "Pond5":         "https://www.pond5.com/dashboard/index/sales",
}

@flask_app.route('/api/inspector/start', methods=['POST'])
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

@flask_app.route('/api/inspector/stop', methods=['POST'])
def api_inspector_stop():
    # only set the flag — the inspector thread closes the browser from its own greenlet
    _inspector_state["running"] = False
    return jsonify({"ok": True})

@flask_app.route('/api/inspector/stream')
def api_inspector_stream():
    import time as _time
    last = [0]
    def _gen():
        while True:
            with _inspector_lock:
                msgs = _inspector_log[last[0]:]
                running = _inspector_state["running"]
            for msg in msgs:
                yield f"data: {json.dumps({'msg': msg})}\n\n"
            last[0] += len(msgs)
            if not running and not msgs:
                yield f"data: {json.dumps({'done': True})}\n\n"
                break
            _time.sleep(0.3)
    from flask import Response
    return Response(_gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@flask_app.route('/api/inspector/status')
def api_inspector_status():
    return jsonify(_inspector_state)

@flask_app.route('/img/cache/<aid>')
def img_cache_serve(aid):
    """Serve img_cache/aid.jpg, or try to fetch and cache on-demand if missing."""
    from flask import send_file, abort, redirect
    import requests as _req
    p = os.path.join(CACHE_DIR, f"{aid}.jpg")
    if os.path.exists(p):
        return send_file(p, mimetype='image/jpeg')
    # Try to fetch thumb_url from DB and cache it (resize to 400×400 for consistency)
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as _c:
            row = _c.execute("SELECT thumb_url FROM sales WHERE asset_id=? AND thumb_url!='' LIMIT 1", (aid,)).fetchone()
        if row and row[0]:
            raw_url = row[0]
            # Prefer higher-res variant for Adobe CDN URLs
            if '_F_' in raw_url:
                raw_url = re.sub(r'\d{3}_F_', '500_F_', raw_url)
            r = _req.get(raw_url, timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200 and raw_url != row[0]:
                r = _req.get(row[0], timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code == 200 and r.content[:2] == b'\xff\xd8':
                img = Image.open(BytesIO(r.content)).convert('RGB')
                img = ImageOps.fit(img, (400, 400), Image.Resampling.LANCZOS)
                img.save(p, 'JPEG', quality=88)
                return send_file(p, mimetype='image/jpeg')
    except Exception:
        pass
    abort(404)

@flask_app.route('/img/placeholder')
def img_placeholder():
    """Tiny gray JPEG placeholder for missing thumbnails."""
    from flask import Response
    import base64
    # 1×1 gray JPEG
    b64 = (
        '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkS'
        'Ew8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJ'
        'CQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy'
        'MjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf/'
        'EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/'
        'aAAwDAQACEQMRAD8AJQAB/9k='
    )
    data = base64.b64decode(b64)
    return Response(data, mimetype='image/jpeg',
                    headers={'Cache-Control': 'public, max-age=86400'})

@flask_app.route('/img/ms/<fname>')
def img_ms_serve(fname):
    """Serve img_cache_ms/fname.jpg для Svelte UI."""
    from flask import send_file, abort
    p = os.path.join(MS_CACHE_DIR, f"{fname}.jpg")
    if not os.path.exists(p): abort(404)
    return send_file(p, mimetype='image/jpeg')

_STOCK_FAVICON_URLS = {
    'adobe stock':   'https://stock.adobe.com/favicon.ico',
    'shutterstock':  'https://www.shutterstock.com/favicon.ico',
    'getty images':  'https://www.google.com/s2/favicons?domain=gettyimages.com&sz=64',
    'istock':        'https://www.google.com/s2/favicons?domain=istockphoto.com&sz=64',
    'istockphoto':   'https://www.google.com/s2/favicons?domain=istockphoto.com&sz=64',
    'depositphotos': 'https://depositphotos.com/favicon.ico',
}

@flask_app.route('/img/stock-icon/<name>')
def img_stock_icon(name):
    """Fetch, cache and serve stock site favicon as 32×32 PNG."""
    from flask import send_file, abort
    safe = re.sub(r'[^a-z0-9]', '_', name.lower())
    p = os.path.join(ICON_CACHE_DIR, f"{safe}.png")
    if not os.path.exists(p):
        url = _STOCK_FAVICON_URLS.get(name.lower())
        if not url:
            abort(404)
        try:
            r = req_lib.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                abort(404)
            img = Image.open(BytesIO(r.content)).convert('RGBA')
            img = img.resize((32, 32), Image.Resampling.LANCZOS)
            img.save(p, 'PNG')
        except Exception as e:
            _app_log(f"[stock-icon] failed {url}: {e}")
            abort(404)
    from flask import make_response
    resp = make_response(send_file(p, mimetype='image/png'))
    resp.headers['Cache-Control'] = 'public, max-age=604800'
    return resp

# ── Perceptual hash (dHash) for cross-stock visual matching ──────────────────
# _dhash_from_path → utils.py

# _save_asset_meta, load_img, load_match_thumb, load_img_async → image_utils.py
# load_groups, save_groups, load_ms_library, save_ms_library → image_utils.py
# ═══════════════════════════════════════════════════════════
# TAB 0 — FEED (стрічка продажів)
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    print(f"🚀 Flask запущено на http://127.0.0.1:{FLASK_PORT}")
    flask_app.run(host='0.0.0.0', port=FLASK_PORT, use_reloader=False, threaded=True)