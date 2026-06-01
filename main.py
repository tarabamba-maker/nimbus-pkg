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
flask_app.register_blueprint(images_bp)
flask_app.register_blueprint(feed_bp)
flask_app.register_blueprint(sync_bp)
flask_app.register_blueprint(groups_bp)

# _save_record → sync_state.py


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

# _query_earnings_batch → app_globals.py

# groups routes → routes/groups.py
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

if __name__ == "__main__":
    init_db()
    print(f"🚀 Flask запущено на http://127.0.0.1:{FLASK_PORT}")
    flask_app.run(host='0.0.0.0', port=FLASK_PORT, use_reloader=False, threaded=True)