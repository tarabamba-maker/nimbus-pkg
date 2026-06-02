"""
main.py — Flask app entry point. Init + blueprint registration only.
All routes live in routes/. All business logic lives in their respective modules.
"""

import os
import ssl
import sys

# Platform detection
IS_MAC = sys.platform == 'darwin'
IS_WIN = sys.platform == 'win32'

# Windows: force UTF-8 stdout/stderr (default cp1252 crashes on emoji log lines)
if IS_WIN:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

# Windows: suppress cmd-window flashes for subprocess calls
_SUBPROC_NOWINDOW = ({'creationflags': 0x0800_0000} if IS_WIN else {})

ssl._create_default_https_context = ssl._create_unverified_context

# Raise file descriptor limit (macOS default 256 → too many open files with 5 parallel collectors)
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

# Load .env (dev mode)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ── Path constants ────────────────────────────────────────────────────────────
# ⚠️ DO NOT REVERT _BASE_DIR to os.path.dirname(__file__).
# Tauri-packaged main.py lives INSIDE the .app bundle — replaced on every update.
# STOCK_DATA_DIR points to a stable user-writable location.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR   = os.environ.get("STOCK_DATA_DIR", _SCRIPT_DIR)
os.makedirs(_BASE_DIR, exist_ok=True)

# ── Migration (DO NOT DELETE) ─────────────────────────────────────────────────
# Copies existing data from old location on first launch after STOCK_DATA_DIR switch.
def _migrate_from(old_dir):
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

for _cand in (_SCRIPT_DIR, os.path.expanduser("~/Desktop/Stock_Automation")):
    _migrate_from(_cand)

# Ensure dirs exist
for _d in ("img_cache", "img_cache_match", "img_cache_ms", "img_cache_icons", "recipes"):
    os.makedirs(os.path.join(_BASE_DIR, _d), exist_ok=True)

FLASK_PORT = 8000

# ── Playwright browser check ──────────────────────────────────────────────────
def _ensure_playwright_browsers():
    import subprocess
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as _pw:
            if IS_WIN:
                browser = _pw.chromium.launch(headless=True, channel="chrome")
            else:
                browser = _pw.chromium.launch(headless=True)
            browser.close()
            return
    except Exception as ex:
        if IS_WIN:
            print(f"[Playwright] System Chrome launch failed: {ex} — install Google Chrome.")
            return
        print(f"[Playwright] Browser launch failed: {ex} — installing chromium...")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "chromium-headless-shell"],
            check=False, timeout=300, **_SUBPROC_NOWINDOW)
        print("[Playwright] ✅ Browsers installed")
    except Exception as e:
        print(f"[Playwright] ⚠️ Install failed: {e}")

_ensure_playwright_browsers()

# ── Flask app ─────────────────────────────────────────────────────────────────
from flask import Flask
from flask_cors import CORS

from db import init_db

flask_app = Flask(__name__)
flask_app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB
CORS(flask_app)

# ── Blueprint registration ────────────────────────────────────────────────────
from routes.images  import images_bp
from routes.feed    import feed_bp
from routes.sync    import sync_bp
from routes.groups  import groups_bp
from routes.matching import matching_bp
from routes.admin   import admin_bp

flask_app.register_blueprint(images_bp)
flask_app.register_blueprint(feed_bp)
flask_app.register_blueprint(sync_bp)
flask_app.register_blueprint(groups_bp)
flask_app.register_blueprint(matching_bp)
flask_app.register_blueprint(admin_bp)

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print(f"🚀 Flask запущено на http://127.0.0.1:{FLASK_PORT}")
    flask_app.run(host='0.0.0.0', port=FLASK_PORT, use_reloader=False, threaded=True)
