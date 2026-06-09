"""
routes/admin.py — Admin, reset, export/import and cookie endpoints.

Moved verbatim from main.py. No logic changes.
Routes: /api/export, /api/import-raw, /api/import-chrome-cookies,
        /api/full-reset, /api/rebuild-from-db, /api/rebuild-groups,
        /api/reset-db, /api/reset, /api/deduplicate
"""

import glob
import os
import shutil
import sqlite3
import tempfile
import threading
import zipfile

from flask import Blueprint, jsonify, request, send_file

from app_globals import (
    _BASE_DIR, CACHE_DIR, DB_NAME, ICON_CACHE_DIR,
    MATCH_CACHE_DIR, MS_CACHE_DIR, RECIPES_DIR,
    _load_matches,
)
from config import STOCK_URLS
from cookies import (
    _STOCK_COOKIE_DOMAINS,
    _chrome_cookies_path, _detect_default_browser,
    _import_cookies_for_stock, _inject_cookies_via_playwright,
    _is_chrome_running, _is_safari_running,
    _parse_safari_binarycookies, _windows_browser_login,
    _stock_has_valid_session,
)
from db import init_db
from image_utils import load_groups, save_groups
from orchestrator import _collect_one_stock_global
from sync_state import (
    _app_log, _sync_log, _sync_state, _sync_stop_flag,
)

import sys
IS_MAC = sys.platform == 'darwin'
IS_WIN = sys.platform == 'win32'

admin_bp = Blueprint('admin', __name__)


# ── Export / Import ───────────────────────────────────────────────────────────

@admin_bp.route('/api/export', methods=['GET'])
def api_export():
    """Pack sales.db + recipes/ into a ZIP and stream it."""
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix='.zip', prefix='nimbus_backup_')
        os.close(tmp_fd)
        try:
            with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                if os.path.exists(DB_NAME):
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
            resp = send_file(tmp_path, mimetype='application/zip',
                             as_attachment=True,
                             download_name='stock_aggregator_backup.zip',
                             conditional=False)
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


@admin_bp.route('/api/import-raw', methods=['POST'])
def api_import_raw():
    """Unpack raw-bytes ZIP body (Content-Type: application/zip)."""
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


# ── Cookies ───────────────────────────────────────────────────────────────────

@admin_bp.route('/api/import-chrome-cookies', methods=['POST'])
def api_import_chrome_cookies():
    """Imports cookies from user's native browser → all stock profiles."""
    data = request.get_json(force=True, silent=True) or {}
    requested = (data.get('source') or '').lower()
    stocks = data.get('stocks') or list(_STOCK_COOKIE_DOMAINS.keys())

    if IS_WIN or IS_MAC:
        # Open a real Chrome window with the app's seed profile — user logs in once,
        # sessions persist and are picked up by _load_browser_cookies() → collectors.
        # Only open tabs for stocks WITHOUT a live session — already-logged-in stocks
        # don't need re-login (and re-opening them risks re-challenging DataDome).
        pending = [s for s in stocks if not _stock_has_valid_session(s)]
        already = [s for s in stocks if s not in pending]
        if not pending:
            return jsonify({'status': 'ok', 'source': 'app-browser-login',
                            'total_imported': 0, 'per_stock': [],
                            'already_logged_in': already,
                            'msg': 'All stocks already logged in'})
        results = _windows_browser_login(pending)
        total = sum(r.get('imported', 0) for r in results)
        return jsonify({'status': 'ok', 'source': 'app-browser-login',
                        'total_imported': total, 'per_stock': results,
                        'already_logged_in': already})

    safari_cookies_path = (
        os.path.expanduser("~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies")
        if IS_MAC else ''
    )
    chrome_cookies_path = _chrome_cookies_path()

    if requested in ('safari', 'chrome'):
        source = requested
        if source == 'safari' and not IS_MAC:
            return jsonify({'status': 'error',
                            'msg': 'Safari is only available on macOS. Use Chrome on Windows.'}), 400
    else:
        if IS_WIN:
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
            if 'Operation not permitted' in str(e) or 'Errno 1' in str(e):
                return jsonify({'status': 'error',
                    'msg': 'macOS блокує доступ до Safari cookies. '
                           'System Settings → Privacy & Security → Full Disk Access → '
                           'додай Stock Automation.app → перезапусти застосунок.'}), 403
            return jsonify({'status': 'error', 'msg': f'Safari parse: {e}'}), 500

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

    if _is_chrome_running():
        return jsonify({'status': 'error',
                        'msg': 'Google Chrome запущений — закрий повністю (Cmd+Q) і спробуй знову'}), 409
    results = []
    for s in stocks:
        results.append(_import_cookies_for_stock(s, chrome_cookies_path))
    total = sum(r.get('imported', 0) for r in results)
    return jsonify({'status': 'ok', 'source': 'chrome',
                    'total_imported': total, 'per_stock': results})


# ── Reset / Rebuild ───────────────────────────────────────────────────────────

@admin_bp.route('/api/full-reset', methods=['POST'])
def api_full_reset():
    """FULL wipe — like fresh install. Body: { "confirm": "RESET" }."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get('confirm') != 'RESET':
        return jsonify({'status': 'error', 'msg': 'send {"confirm":"RESET"} to proceed'}), 400

    _sync_stop_flag[0] = True
    _sync_state['running'] = False

    try:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.execute("DELETE FROM sales")
            c.execute("DELETE FROM asset_meta")
            c.commit()
            c.execute("VACUUM")
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'db: {e}'}), 500

    if os.path.isdir(RECIPES_DIR):
        for entry in os.listdir(RECIPES_DIR):
            fp = os.path.join(RECIPES_DIR, entry)
            try:
                if os.path.isfile(fp): os.remove(fp)
                elif os.path.isdir(fp): shutil.rmtree(fp)
            except Exception: pass

    profiles_removed = 0
    for entry in os.listdir(_BASE_DIR):
        if (entry.startswith('chrome_profile') or entry.endswith('_profile')
                or entry.startswith('getty_profile')):
            fp = os.path.join(_BASE_DIR, entry)
            if os.path.isdir(fp):
                try:
                    shutil.rmtree(fp)
                    profiles_removed += 1
                except Exception: pass

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


@admin_bp.route('/api/rebuild-from-db', methods=['POST'])
def api_rebuild_from_db():
    """Rebuild groups & matches from EXISTING DB data. Body: { "confirm": "REBUILD" }."""
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
        from routes.matching import api_rebuild_matches
        # force=True → wipes incremental state + snapshot + cross-stock matches and
        # re-clusters the WHOLE catalog from scratch. Without it the rebuild would
        # early-exit 'cached' and leave the just-deleted groups empty (the recurring
        # "rebuild-from-db does nothing" bug).
        resp = api_rebuild_matches(force=True)
        return jsonify({'status': 'ok', 'result': resp.get_json()})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@admin_bp.route('/api/rebuild-groups', methods=['POST'])
def api_rebuild_groups():
    """Rebuild group structure from scratch. Body: { "confirm": "REBUILD" }."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get('confirm') != 'REBUILD':
        return jsonify({'status': 'error', 'msg': 'send {"confirm":"REBUILD"} to proceed'}), 400

    for fname in ('photo_groups.json', '_manual_overrides.json',
                  '_cross_stock_matches.json', 'ms_library.json',
                  '_ms_dirs_state.json', '_matches_state.json',
                  '_ms_group_snapshot.json'):
        fp = os.path.join(RECIPES_DIR, fname)
        if os.path.exists(fp):
            try: os.remove(fp)
            except Exception: pass

    if os.path.isdir(MS_CACHE_DIR):
        for f in os.listdir(MS_CACHE_DIR):
            try: os.remove(os.path.join(MS_CACHE_DIR, f))
            except Exception: pass
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as _c:
            _c.execute("DELETE FROM ms_meta")
            _c.commit()
    except Exception: pass

    def _run():
        try:
            ms_url = STOCK_URLS.get("Microstock+", "https://microstock.plus/myfiles")
            _sync_state["running"] = True
            _sync_state["log"] = []
            _sync_stop_flag[0] = False
            _collect_one_stock_global("Microstock+", ms_url)
            try:
                from main import flask_app
                from routes.matching import api_rebuild_matches
                with flask_app.app_context():
                    resp = api_rebuild_matches()
                _sync_log(f"✅ Groups rebuilt: {resp.get_json()}")
            except Exception as ex:
                _sync_log(f"⚠️ rebuild-matches failed: {ex}")
        finally:
            _sync_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'ok', 'msg': 'MS+ sync + rebuild started in background'})


@admin_bp.route('/api/reset-db', methods=['POST'])
def api_reset_db():
    """Full data wipe, keeps chrome_profile* and stock_colors.json. Body: { "confirm": "RESET" }."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get('confirm') != 'RESET':
        return jsonify({'status': 'error', 'msg': 'send {"confirm":"RESET"} to proceed'}), 400

    _sync_stop_flag[0] = True
    _sync_state['running'] = False

    removed_counts = {}

    try:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.execute("DELETE FROM sales")
            c.execute("DELETE FROM asset_meta")
            c.commit()
            c.execute("VACUUM")
        removed_counts['db_tables'] = 'cleared'
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'db: {e}'}), 500

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

    for legacy in (os.path.join(_BASE_DIR, 'ms_library.json'),):
        if os.path.exists(legacy):
            try: os.remove(legacy)
            except Exception: pass

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


@admin_bp.route('/api/reset', methods=['POST'])
def api_reset():
    """Full account reset: wipes sales DB, all browser profiles, image caches.
    Keeps user groups, stock colors, and ms_library. Body: { "confirm": "RESET" }."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get('confirm') != 'RESET':
        return jsonify({'status': 'error', 'msg': 'send {"confirm":"RESET"} to proceed'}), 400

    _sync_state['running'] = False

    removed = []
    errors  = []

    try:
        db_path = os.path.join(_BASE_DIR, 'sales.db')
        if os.path.exists(db_path):
            os.remove(db_path)
        init_db()
        removed.append('sales.db')
    except Exception as e:
        errors.append(f'sales.db: {e}')

    profile_patterns = [
        os.path.join(_BASE_DIR, 'chrome_profile*'),
        os.path.join(_BASE_DIR, '*_profile'),
        os.path.join(_BASE_DIR, '*_profile_*'),
    ]
    for pat in profile_patterns:
        for d in glob.glob(pat):
            if os.path.isdir(d):
                try:
                    shutil.rmtree(d)
                    removed.append(os.path.basename(d))
                except Exception as e:
                    errors.append(f'{os.path.basename(d)}: {e}')

    cache_dirs = [
        os.path.join(_BASE_DIR, 'img_cache'),
        os.path.join(_BASE_DIR, 'img_cache_match'),
        os.path.join(_BASE_DIR, 'img_cache_ms'),
        os.path.join(_BASE_DIR, 'img_cache_icons'),
    ]
    for d in cache_dirs:
        if os.path.isdir(d):
            try:
                shutil.rmtree(d)
                os.makedirs(d)
                removed.append(os.path.basename(d))
            except Exception as e:
                errors.append(f'{os.path.basename(d)}: {e}')

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


@admin_bp.route('/api/deduplicate', methods=['POST'])
def api_deduplicate():
    """Remove duplicate sales rows + deduplicate group IDs."""
    result = {}

    try:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            before = c.execute('SELECT COUNT(*) FROM sales').fetchone()[0]
            # ⚠️ DEDUP ONLY EXACT DUPLICATES — full datetime + price. The old
            # `GROUP BY stock, asset_id, DATE(date)` collapsed EVERY same-day sale of
            # a photo into one (a 6564-download Adobe top-seller → 1108), destroying
            # real subscription sales. A photo legitimately sells many times per day
            # at the same/different price — only an identical (stock, asset_id, exact
            # timestamp, price) row is a true duplicate.
            c.execute('''
                DELETE FROM sales WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM sales
                    GROUP BY stock, asset_id, date, price
                )
            ''')
            c.commit()
            after = c.execute('SELECT COUNT(*) FROM sales').fetchone()[0]
        result['sales_removed'] = before - after
        result['sales_remaining'] = after
    except Exception as e:
        result['sales_error'] = str(e)

    try:
        groups = load_groups()
        clean = {}
        for name in sorted(groups.keys()):
            clean[name] = list(dict.fromkeys(str(a) for a in groups[name]))

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
