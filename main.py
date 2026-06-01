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
    _sync_log, _save_record, _app_log,
    get_headless, set_headless, _headless_lock,
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
from orchestrator import _run_collector_global, _collect_one_stock_global, _sync_all_global
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
# _app_log, _log_file_handle → sync_state.py

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


from config import STOCK_URLS
from app_globals import (
    _RELEVANT_STOCKS, STOCKS, _STOCK_KEY,
    _load_matches, _save_matches, _load_overrides, _save_overrides,
    _query_earnings_batch,
)

# ═══════════════════════════════════════════════════════════
# БД
# ═══════════════════════════════════════════════════════════
# _STOCK_KEY, STOCKS → app_globals.py
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

# ── Blueprint registration ────────────────────────────────────────────────────
from routes.images import images_bp
from routes.feed   import feed_bp
from routes.sync   import sync_bp
from routes.groups import groups_bp
from routes.matching import matching_bp
from routes.admin   import admin_bp
flask_app.register_blueprint(images_bp)
flask_app.register_blueprint(feed_bp)
flask_app.register_blueprint(sync_bp)
flask_app.register_blueprint(groups_bp)
flask_app.register_blueprint(matching_bp)
flask_app.register_blueprint(admin_bp)

# _save_record → sync_state.py


# admin routes → routes/admin.py

# _hash_based_matches → matching_engine.py

# matching routes → routes/matching.py

if __name__ == "__main__":
    init_db()
    print(f"🚀 Flask запущено на http://127.0.0.1:{FLASK_PORT}")
    flask_app.run(host='0.0.0.0', port=FLASK_PORT, use_reloader=False, threaded=True)