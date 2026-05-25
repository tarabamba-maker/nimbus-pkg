import ssl, time, threading, sqlite3, os, re, json, base64, atexit
from io import BytesIO
from datetime import datetime, timedelta

ssl._create_default_https_context = ssl._create_unverified_context

# ── Логування у файл + термінал ──────────────────────────────
_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.log")
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
    import subprocess, sys
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as _pw:
            _pw.chromium.executable_path  # перевіряємо наявність
    except Exception:
        print("[Playwright] Браузери не знайдено — встановлюю...")
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False)
        print("[Playwright] ✅ Встановлено")

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

def _adobe_clean_thumb_url(thumb_url: str) -> str:
    """
    Конвертує будь-який Adobe ftcdn.net URL у чисту 110px версію без watermark.
    as2.ftcdn.net/jpg/.../110_F_{id}_{hash}.jpg
    """
    if not thumb_url or "ftcdn.net" not in thumb_url:
        return thumb_url
    url = re.sub(r'/\d+_F_', '/110_F_', thumb_url)
    url = re.sub(r'https?://[^/]+\.ftcdn\.net', 'https://as2.ftcdn.net', url)
    return url

RECIPES_DIR            = os.path.join(_BASE_DIR, "recipes")
MATCHES_FILE           = os.path.join(RECIPES_DIR, "_cross_stock_matches.json")
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

def init_db():
    with sqlite3.connect(DB_NAME, timeout=15) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT, photo_name TEXT,
            stock TEXT DEFAULT "Adobe Stock",
            price REAL, thumb_url TEXT,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            filename TEXT)''')
        try: c.execute("ALTER TABLE sales ADD COLUMN filename TEXT")
        except Exception: pass

        # Per-asset perceptual hash + dominant RGB + aspect_ratio for visual matching.
        c.execute('''CREATE TABLE IF NOT EXISTS asset_meta (
            stock TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            thumb_hash TEXT,
            aspect_ratio REAL,
            r INTEGER, g INTEGER, b INTEGER,
            updated_at TEXT,
            PRIMARY KEY (stock, asset_id))''')
        for col, sqltype in [("r", "INTEGER"), ("g", "INTEGER"), ("b", "INTEGER")]:
            try: c.execute(f"ALTER TABLE asset_meta ADD COLUMN {col} {sqltype}")
            except Exception: pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_asset_meta_hash ON asset_meta(thumb_hash)")

        # MS+ reference thumbnails (img_cache_ms/*.jpg) — used as ground-truth base
        # for visual matching. fname = disk filename WITHOUT .jpg extension.
        c.execute('''CREATE TABLE IF NOT EXISTS ms_meta (
            fname TEXT PRIMARY KEY,
            thumb_hash TEXT,
            aspect_ratio REAL,
            r INTEGER, g INTEGER, b INTEGER,
            updated_at TEXT)''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_ms_meta_hash ON ms_meta(thumb_hash)")

        # Таблиця фотографій — єдине джерело правди
        c.execute('''CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY,
            stock_ids TEXT DEFAULT "{}",
            thumb_url TEXT DEFAULT "",
            ms_folder TEXT DEFAULT "",
            groups TEXT DEFAULT "[]",
            earnings TEXT DEFAULT "{}",
            updated_at TEXT DEFAULT "")''')
        # Індекс (stock_key, asset_id) → photo_id для швидкого lookup
        c.execute('''CREATE TABLE IF NOT EXISTS photo_assets (
            stock_key TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            photo_id TEXT NOT NULL,
            PRIMARY KEY (stock_key, asset_id))''')
        c.commit()


def is_already_saved(stock, asset_id, price, date_str):
    """Returns True if this exact sale already exists in the DB.

    Two-pass strategy to handle both old (date-only) and new (datetime) records:
    1. If date_str has a time component (ISO from Adobe): exact datetime match
       against records stored with time.  Different sales of the same photo on
       the same day have different timestamps → correctly allowed.
    2. Fallback: price±0.005 + day match, restricted to old date-only rows
       (LENGTH(date)=10).  Catches re-synced duplicates stored before we kept time.
    """
    if not asset_id:
        return False
    try:
        raw = str(date_str)
        day = raw[:10]
        for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                day = datetime.strptime(raw[:10], fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                pass
        p = float(price) if price else 0.0
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            # Pass 1: exact datetime (new records stored with HH:MM:SS)
            if 'T' in raw or (' ' in raw and len(raw) > 10):
                dt_norm = raw.replace('T', ' ').split('+')[0].split('Z')[0][:19]
                row = c.execute(
                    'SELECT 1 FROM sales WHERE stock=? AND asset_id=? AND date=?',
                    (stock, str(asset_id), dt_norm)
                ).fetchone()
                if row:
                    return True
            # Pass 2: price+day fallback for old date-only records
            row = c.execute(
                'SELECT 1 FROM sales WHERE stock=? AND asset_id=? AND date LIKE ? '
                'AND LENGTH(date)=10 AND ABS(price - ?) < 0.005',
                (stock, str(asset_id), day + '%', p)
            ).fetchone()
        return row is not None
    except Exception:
        return False

def save_to_db(d):
    try:
        stock = d.get('stock', 'Adobe Stock')
        aid   = str(d.get('asset_id', ''))
        price = float(d.get('price') or 0)
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.execute(
                'INSERT INTO sales (asset_id,photo_name,stock,price,thumb_url,date,filename) '
                'VALUES (?,?,?,?,?,?,?)',
                (aid, d.get('photo_name'), stock, price,
                 d.get('thumb_url'), d.get('date'), d.get('filename')))
            c.commit()
    except Exception as e:
        print(f"[DB] {e}")

# ═══════════════════════════════════════════════════════════
# FLASK
# ═══════════════════════════════════════════════════════════
flask_app = Flask(__name__)
# Allow large backup imports (default Flask limit ≈ 16 MB; our backups can be ~30 MB)
flask_app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB cap
CORS(flask_app)

@flask_app.route('/update', methods=['POST'])
def api_update():
    d = request.json
    if not d: return jsonify({"status": "error"}), 400
    raw_date = d.get('date'); dt = datetime.now()
    if raw_date:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
                    "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw_date.split('+')[0].split('Z')[0], fmt)
                break
            except Exception: pass
    d['date']  = dt.strftime("%Y-%m-%d %H:%M:%S")
    d['stock'] = d.get('stock', 'Adobe Stock')
    save_to_db(d)
    return jsonify({"status": "success"}), 200

# ═══════════════════════════════════════════════════════════
# TAURI API — використовується Svelte UI
# ═══════════════════════════════════════════════════════════

# Глобальний стан синку для /api/sync/status
_sync_state: dict = {"running": False, "log": [], "progress": ""}
_sync_stop_flag: list  = [False]   # [0] = True → зупинити синк
_sync_all_active: list = [False]   # [0] = True → синк вже запущено
_headless_mode: bool   = True
_headless_lock = threading.Lock()
_sync_log_lock = threading.Lock()  # guards _sync_state["log"] reads/writes

def _sync_log(msg: str):
    with _sync_log_lock:
        _sync_state["log"].append(msg)
        _sync_state["log"] = _sync_state["log"][-200:]
        _sync_state["progress"] = msg


# ───────────────────────────────────────────────────────────
# Playwright-колектори (top-level, без Flet)
# ───────────────────────────────────────────────────────────

_STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['uk-UA','uk','en-US','en']});
    window.chrome = {runtime: {}};
    Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
"""

def _apply_stealth(ctx):
    ctx.add_init_script(_STEALTH_JS)

_LOGIN_SIGNALS = ["auth", "sign-in", "login", "signin", "ims-na1"]

def _is_login_url(url_str):
    return any(x in url_str.lower() for x in _LOGIN_SIGNALS)

def _open_browser_context(p, profile_dir, headless, off_screen=False):
    extra = ["--window-position=0,2000", "--window-size=1280,900"] if off_screen else []
    return p.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=headless,
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
        ] + extra
    )

def _do_login_flow_global(p, profile_dir, target_url, stock_label, wait_cond):
    """Відкриває видимий браузер, чекає поки юзер залогіниться і закриє вікно."""
    _sync_log(f"🔒 {stock_label}: потрібна авторизація — відкриваю браузер...")
    vis = _open_browser_context(p, profile_dir, headless=False)
    _apply_stealth(vis)
    vp = vis.pages[0] if vis.pages else vis.new_page()
    try:
        vp.goto(target_url, wait_until=wait_cond, timeout=90000)
    except Exception:
        pass
    _sync_log(f"👤 {stock_label}: залогуйся і ЗАКРИЙ ВІКНО БРАУЗЕРА — збір продовжиться автоматично (макс 10 хв)")
    try:
        # 10-min cap — if user walks away, sync still recovers instead of hanging forever
        vis.wait_for_event("close", timeout=600000)
    except Exception:
        _sync_log(f"⏱ {stock_label}: 10-хв timeout — закриваю браузер примусово")
    finally:
        try: vis.close()
        except Exception: pass
    _sync_log(f"✅ {stock_label}: браузер закрито, продовжую збір...")


def _adobe_api_collect_global(pw_page):
    """Збирає Adobe Stock через API (browser fetch — обхід CSRF)."""
    _sync_log("🚀 Adobe API: навігація на contributor portal...")

    adobe_url = "https://contributor.stock.adobe.com/en/insights/sales-earnings"
    if "contributor.stock.adobe.com" not in pw_page.url:
        pw_page.goto(adobe_url, wait_until="networkidle", timeout=45000)
        time.sleep(3)

    if any(x in pw_page.url.lower() for x in ["login", "signin", "auth", "ims-na1"]):
        _sync_log("🔒 Adobe: потрібна авторизація")
        return "needs_login"

    def _fetch_page(pg):
        ts = int(time.time() * 1000)
        url = f"/en/insights/sales-earnings?limit=1000&page={pg}&pv={ts}"
        js = f"""
        async () => {{
            try {{
                const r = await fetch("{url}", {{
                    headers: {{"accept": "application/json", "x-requested-with": "XMLHttpRequest"}},
                    credentials: "same-origin"
                }});
                if (!r.ok) return {{error: r.status}};
                const text = await r.text();
                try {{ return JSON.parse(text); }}
                catch(e) {{ return {{error: "not_json", preview: text.slice(0,200)}}; }}
            }} catch(e) {{ return {{error: e.toString()}}; }}
        }}"""
        try:
            return pw_page.evaluate(js)
        except Exception as ex:
            return {"error": str(ex)}

    _sync_log("🔍 Тест API (page=1)...")
    td = _fetch_page(1)
    if "error" in td:
        preview = td.get("preview", "")
        if td["error"] == "not_json" and any(x in preview.lower() for x in ["sign-in","login","ims-na1","adobelogin"]):
            _sync_log("🔒 Adobe: сесія закінчилась — потрібна авторизація")
            return "needs_login"
        _sync_log(f"🛑 Adobe тест провалився: {td['error']}")
        return None

    pagination = td.get("view", {}).get("pagination", {})
    total_pages = pagination.get("pages", 1)
    total_items = pagination.get("total", 0)
    _sync_log(f"   → сторінок: {total_pages}, записів всього: {total_items}")

    total_saved = 0
    all_seen = 0
    for pg in range(1, total_pages + 1):
        data = td if pg == 1 else _fetch_page(pg)
        if "error" in data:
            _sync_log(f"  ⚠️ page={pg}: {data['error']}")
            continue
        history = data.get("sales", {}).get("history", [])
        _sync_log(f"📄 page={pg}/{total_pages}: {len(history)} записів")

        page_new = 0
        page_old = 0
        stop_early = False
        for item in history:
            asset_id   = str(item.get("id", ""))
            price      = float(item.get("commissionAmount", 0))
            thumb      = item.get("thumbnailUrl", "")
            sale_full  = item.get("saleDate", "")        # full ISO: "2026-05-03T01:13:34+00:00"
            sale_dt    = sale_full[:10]                  # "2026-05-03" — display/filter only
            photo_name = item.get("title") or item.get("originalName") or asset_id
            orig_name  = item.get("originalName") or ""
            fname_no_ext = orig_name.rsplit(".", 1)[0] if orig_name and "." in orig_name else orig_name
            if not asset_id or not sale_dt:
                continue
            all_seen += 1
            # Dedup on full datetime: same sale re-synced has same timestamp;
            # different sales of the same photo have different timestamps.
            if is_already_saved("Adobe Stock", asset_id, price, sale_full):
                page_old += 1
                if pg == 1 and page_old >= 100:
                    _sync_log(f"⏹ Adobe: 100 збережених на стор.1 — зупиняємось")
                    stop_early = True
                    break
                continue
            rec = {"asset_id": asset_id, "photo_name": photo_name, "stock": "Adobe Stock",
                   "price": price, "thumb_url": thumb, "date": sale_full,
                   "filename": fname_no_ext}
            if thumb:
                load_img_async(asset_id, thumb, None, is_adobe=True)
            _http_executor.submit(
                lambda dd=rec: req_lib.post(
                    f"http://127.0.0.1:{FLASK_PORT}/update", json=dd, timeout=5))
            total_saved += 1
            page_new += 1

        if stop_early:
            break
        if pg < total_pages:
            time.sleep(0.3)

    _sync_log(f"✅ Adobe API (останні): {total_saved} нових з {all_seen} перевірених")

    # ── Історичний збір по 90-денних чанках назад ────────────────────────
    proc_file = os.path.join(RECIPES_DIR, "_processed_dates.json")
    try:
        with open(proc_file) as f:
            all_proc = json.load(f)
    except Exception:
        all_proc = {}
    adobe_done = set(all_proc.get("Adobe Stock", []))

    def _fetch_range(start_str, end_str, pg):
        ts = int(time.time() * 1000)
        url = (f"/en/insights/sales-earnings"
               f"?end_date={end_str}&start_date={start_str}"
               f"&time_range=day&timestamp={ts}&pv={ts}"
               f"&limit=1000&page={pg}")
        js = f"""async () => {{
            try {{
                const r = await fetch("{url}", {{
                    headers: {{"accept": "application/json", "x-requested-with": "XMLHttpRequest"}},
                    credentials: "same-origin"
                }});
                if (!r.ok) return {{error: r.status}};
                const text = await r.text();
                try {{ return JSON.parse(text); }}
                catch(e) {{ return {{error: "not_json", preview: text.slice(0,100)}}; }}
            }} catch(e) {{ return {{error: e.toString()}}; }}
        }}"""
        try:
            return pw_page.evaluate(js)
        except Exception as ex:
            return {"error": str(ex)}

    def _months_in_range(start, end):
        months = set()
        d = start.replace(day=1)
        while d <= end:
            months.add(d.strftime("%Y-%m"))
            d = d.replace(month=d.month % 12 + 1) if d.month < 12 else d.replace(year=d.year+1, month=1)
        return months

    now = datetime.now()
    chunk_end = now - timedelta(days=1)
    cutoff    = now - timedelta(days=365 * 10)
    hist_saved = 0

    while chunk_end > cutoff:
        chunk_start = max(chunk_end - timedelta(days=89), cutoff)
        months = _months_in_range(chunk_start, chunk_end)

        if months.issubset(adobe_done):
            chunk_end = chunk_start - timedelta(days=1)
            continue

        s_str = chunk_start.strftime("%Y-%m-%d")
        e_str = chunk_end.strftime("%Y-%m-%d")
        _sync_log(f"📅 Adobe: {s_str} → {e_str}")

        d0 = _fetch_range(s_str, e_str, 1)
        if "error" in d0:
            _sync_log(f"  ⚠️ {d0['error']} — пропускаю чанк")
            chunk_end = chunk_start - timedelta(days=1)
            continue

        range_pages = d0.get("view", {}).get("pagination", {}).get("pages", 1)
        range_total = d0.get("view", {}).get("pagination", {}).get("total", 0)
        _sync_log(f"   → {range_total} записів, {range_pages} стор.")

        chunk_new = 0
        for pg in range(1, range_pages + 1):
            d = d0 if pg == 1 else _fetch_range(s_str, e_str, pg)
            if "error" in d:
                _sync_log(f"  ⚠️ page={pg}: {d['error']}")
                continue
            for item in d.get("sales", {}).get("history", []):
                asset_id   = str(item.get("id", ""))
                price      = float(item.get("commissionAmount", 0))
                thumb      = item.get("thumbnailUrl", "")
                sale_full  = item.get("saleDate", "")
                sale_dt    = sale_full[:10]
                photo_name = item.get("title") or item.get("originalName") or asset_id
                orig_name  = item.get("originalName") or ""
                fname_no_ext = orig_name.rsplit(".", 1)[0] if orig_name and "." in orig_name else orig_name
                if not asset_id or not sale_dt:
                    continue
                if is_already_saved("Adobe Stock", asset_id, price, sale_full):
                    continue
                rec = {"asset_id": asset_id, "photo_name": photo_name,
                       "stock": "Adobe Stock", "price": price,
                       "thumb_url": thumb, "date": sale_full, "filename": fname_no_ext}
                if thumb:
                    load_img_async(asset_id, thumb, None, is_adobe=True)
                _http_executor.submit(
                    lambda dd=rec: req_lib.post(
                        f"http://127.0.0.1:{FLASK_PORT}/update", json=dd, timeout=5))
                chunk_new += 1
                hist_saved += 1
            if pg < range_pages:
                time.sleep(0.3)

        if chunk_new > 0:
            _sync_log(f"   ✚ {chunk_new} нових")

        adobe_done.update(months)
        all_proc["Adobe Stock"] = sorted(adobe_done)
        with open(proc_file, "w") as f:
            json.dump(all_proc, f, indent=2)

        chunk_end = chunk_start - timedelta(days=1)
        time.sleep(0.5)

    _sync_log(f"✅ Adobe (всього нових): {total_saved + hist_saved}")


def _shutterstock_api_collect_global(pw_page):
    """Збирає Shutterstock через aggregate API + media_stats/day per-photo."""
    _sync_log("🚀 Shutterstock API: старт...")

    if "/earnings" not in pw_page.url:
        pw_page.goto("https://submit.shutterstock.com/earnings",
                     wait_until="networkidle", timeout=45000)
        time.sleep(3)

    if any(x in pw_page.url.lower() for x in ["login", "signin", "auth"]):
        _sync_log("🔒 Shutterstock: потрібна авторизація")
        return

    SS_CATEGORIES = [
        "single_image_and_other", "25_a_day", "on_demand", "enhanced",
        "footage_enhanced", "clip_packs", "unlimited_photo_commission",
        "unlimited_video_commission",
    ]

    def _fetch(url_path):
        js = f"""async () => {{
            try {{
                const r = await fetch("{url_path}", {{
                    headers: {{"accept": "application/json",
                               "x-end-app-name": "contributor-web"}}
                }});
                if (!r.ok) return {{error: r.status}};
                return await r.json();
            }} catch(e) {{ return {{error: e.toString()}}; }}
        }}"""
        try:
            return pw_page.evaluate(js)
        except Exception as ex:
            return {"error": str(ex)}

    proc_file = os.path.join(RECIPES_DIR, "_processed_dates.json")
    try:
        with open(proc_file) as f:
            all_proc = json.load(f)
    except Exception:
        all_proc = {}

    ss_done_months = set(all_proc.get("Shutterstock_months", []))
    old_days = set(all_proc.get("Shutterstock", []))
    if old_days:
        import calendar as _cal_mig
        _months_seen = {}
        for d in old_days:
            try:
                ym = d[:7]
                yr_m, mo_m = int(ym[:4]), int(ym[5:7])
                total_days = _cal_mig.monthrange(yr_m, mo_m)[1]
                _months_seen.setdefault(ym, set()).add(d)
                if len(_months_seen[ym]) >= total_days:
                    ss_done_months.add(ym)
            except Exception:
                pass

    from datetime import date as _date
    import calendar as _cal
    today = _date.today()
    total_saved = 0

    # Auto-detect first-time sync: if DB has 0 Shutterstock records, scan ALL months
    # from 2018-01 to today (full account history). Otherwise stick to last 30 days.
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as _c:
            _ss_count = _c.execute("SELECT COUNT(*) FROM sales WHERE stock='Shutterstock'").fetchone()[0]
    except Exception:
        _ss_count = 0

    if _ss_count == 0:
        _sync_log(f"🔄 Shutterstock: БД пуста — повний історичний збір з 2018-01")
        scan_days = []
        d = _date(2018, 1, 1)
        while d <= today:
            scan_days.append(d)
            d += timedelta(days=1)
        recent_days = list(reversed(scan_days))  # newest first
    else:
        recent_days = [(today - timedelta(days=i)) for i in range(30)]
        _sync_log(f"⏩ Shutterstock: інкрементальний збір — останні 30 днів...")
    stop_early = False

    _agg_cache = {}

    def _get_day_cats(day: _date):
        ym = (day.year, day.month)
        if ym not in _agg_cache:
            agg = _fetch(f"/api/next/v2/earnings/aggregate"
                         f"?aggregation_period=day&year={ym[0]}&month={ym[1]}")
            _agg_cache[ym] = {d.get("date","")[:10]: d
                              for d in agg.get("days", [])} if "error" not in agg else {}
        day_info = _agg_cache[ym].get(day.isoformat(), {})
        return [cat for cat in SS_CATEGORIES
                if isinstance(day_info.get(cat), dict)
                and day_info[cat].get("earnings", 0) > 0]

    for scan_day in recent_days:
        if stop_early:
            break
        date_str = scan_day.isoformat()
        active_cats = _get_day_cats(scan_day)
        if not active_cats:
            continue

        day_new = 0
        day_already = 0
        for cat in active_cats:
            page_n = 1
            while True:
                url_path = (f"/api/next/v2/earnings/media_stats/day"
                            f"?display_column={cat}&date={date_str}"
                            f"&page={page_n}&per_page=100")
                data = _fetch(url_path)
                if "error" in data:
                    _sync_log(f"  ⚠️ {date_str}/{cat} p{page_n}: {data['error']}")
                    break

                items = data.get("media", [])
                if not items:
                    break

                for item in items:
                    asset_id = str(item.get("mediaId", ""))
                    price    = float(item.get("total", 0))
                    thumb    = (item.get("details") or {}).get("previewImageUrl", "")
                    name     = (item.get("details") or {}).get("description", "") or asset_id
                    if not asset_id or price == 0:
                        continue
                    if is_already_saved("Shutterstock", asset_id, price, date_str):
                        day_already += 1
                        continue
                    rec = {"asset_id": asset_id, "stock": "Shutterstock",
                           "price": price, "thumb_url": thumb,
                           "photo_name": name, "date": date_str}
                    if thumb:
                        load_img_async(asset_id, thumb, None)
                    _http_executor.submit(
                        lambda dd=rec: req_lib.post(
                            f"http://127.0.0.1:{FLASK_PORT}/update",
                            json=dd, timeout=5))
                    day_new    += 1
                    total_saved += 1

                if page_n >= data.get("pages", 1):
                    break
                page_n += 1
                time.sleep(0.3)

        if day_new > 0:
            _sync_log(f"  📆 {date_str}: +{day_new} нових")
        elif day_already > 0:
            _sync_log(f"  ✅ {date_str}: вже зібрано ({day_already} записів) — зупиняємось")
            stop_early = True

    all_proc["Shutterstock_last_sync"] = today.isoformat()
    all_proc["Shutterstock_months"] = sorted(ss_done_months)
    all_proc.pop("Shutterstock", None)
    with open(proc_file, "w") as f:
        json.dump(all_proc, f, indent=2)

    _sync_log(f"✅ Shutterstock API: {total_saved} нових записів")


def _depositphotos_collect(pw_page):
    """Collect Depositphotos sales via HTML scraping of /sales/pageN.html?ajax=true"""
    from bs4 import BeautifulSoup
    import re

    MONTH_MAP = {
        "Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06",
        "Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"
    }

    def parse_date(s):
        m = re.match(r"(\w+)\.(\d+),\s*(\d+)", s.strip())
        if not m: return None
        return f"{m.group(3)}-{MONTH_MAP.get(m.group(1),'00')}-{m.group(2).zfill(2)}"

    def parse_price(s):
        s = s.strip().lstrip("$")
        try: return float(s)
        except: return 0.0

    def extract_rows(html):
        if "%%%%" in html:
            html = html.split("%%%%")[-1]
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        for tr in soup.select("table tr"):
            tds = tr.find_all("td")
            if len(tds) < 9:
                continue
            img = tds[0].find("img")
            if not img:
                continue
            asset_id = img.get("data-id", "").strip()
            if not asset_id or asset_id == "-":
                continue
            src = img.get("src", "")
            thumb = ("https:" + src) if src.startswith("//") else src
            date  = parse_date(tds[3].get_text(strip=True))
            price = parse_price(tds[8].get_text(strip=True))
            title = tds[2].get("title", "") or tds[2].get_text(strip=True)
            rows.append({"asset_id": asset_id, "thumb": thumb,
                         "date": date, "price": price, "title": title})
        return rows

    _sync_log("Depositphotos: loading sales page…")
    pw_page.goto("https://depositphotos.com/sales.html",
                 wait_until="domcontentloaded", timeout=30000)
    pw_page.wait_for_timeout(2000)

    if _is_login_url(pw_page.url):
        _sync_log("Depositphotos: ⚠️ not logged in, skipping")
        return

    total_saved = 0
    page_num    = 1
    all_known_streak = 0

    # First-time sync: raise page cap to 500 to pull full history.
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as _c:
            _dp_count = _c.execute("SELECT COUNT(*) FROM sales WHERE stock='Depositphotos'").fetchone()[0]
    except Exception:
        _dp_count = 0
    PAGE_CAP = 500 if _dp_count == 0 else 30
    if _dp_count == 0:
        _sync_log(f"🔄 Depositphotos: БД пуста — повний історичний збір (до {PAGE_CAP} сторінок)")

    while not _sync_stop_flag[0]:
        url = ("/sales.html" if page_num == 1
               else f"/sales/page{page_num}.html?ajax=true")
        _sync_log(f"Depositphotos: page {page_num}…")
        try:
            # 60s timeout per fetch — DataDome can hang requests indefinitely
            pw_page.set_default_timeout(60000)
            html = pw_page.evaluate(f"""async () => {{
                const ctrl = new AbortController();
                const tid = setTimeout(() => ctrl.abort(), 55000);
                try {{
                    const r = await fetch("{url}", {{
                        credentials: "include",
                        signal: ctrl.signal,
                        headers: {{ "x-requested-with": "XMLHttpRequest",
                                   "accept": "text/html, */*; q=0.01" }}
                    }});
                    return await r.text();
                }} finally {{
                    clearTimeout(tid);
                }}
            }}""")
        except Exception as e:
            _sync_log(f"Depositphotos: fetch error page {page_num}: {e}")
            break

        rows = extract_rows(html)
        if not rows:
            _sync_log(f"Depositphotos: page {page_num} empty, done")
            break

        new_in_page = 0
        # Track if the whole page is older than the newest known sale → we've
        # already synced past this point. More robust than exact price match
        # which can fail on Deposit's float rounding.
        page_max_date = ''
        for row in rows:
            if not row["date"]:
                continue
            if row["date"] > page_max_date:
                page_max_date = row["date"]
            if is_already_saved("Depositphotos", row["asset_id"], row["price"], row["date"]):
                continue
            save_to_db({"stock": "Depositphotos", "asset_id": row["asset_id"],
                        "price": row["price"], "date": row["date"],
                        "title": row["title"], "thumb_url": row["thumb"]})
            load_img_async(row["asset_id"], row["thumb"], None, False)
            new_in_page  += 1
            total_saved  += 1

        # Date-based early stop: if the newest row on this page is older than
        # the most recent Deposit sale we already have in DB, every page beyond
        # is guaranteed already-synced. Cheap one-shot SQL lookup per page.
        try:
            with sqlite3.connect(DB_NAME, timeout=15) as _c:
                _last = _c.execute(
                    "SELECT MAX(date) FROM sales WHERE stock='Depositphotos'"
                ).fetchone()[0]
            last_known = (_last or '')[:10]
        except Exception:
            last_known = ''

        _sync_log(f"Depositphotos: page {page_num} → {new_in_page} new (max date {page_max_date})")

        # Stop conditions (any one is enough):
        #   1) two consecutive zero-new pages (original heuristic)
        #   2) this page's newest date is ≤ last sync date we already have AND nothing new
        #   3) we've scanned 30 pages — sane hard cap to never run away
        if new_in_page == 0:
            all_known_streak += 1
            if (all_known_streak >= 2
                    or (last_known and page_max_date and page_max_date <= last_known)):
                _sync_log("Depositphotos: caught up to last known sale, stopping")
                break
        else:
            all_known_streak = 0
        if page_num >= PAGE_CAP:
            _sync_log(f"Depositphotos: hit {PAGE_CAP}-page cap, stopping")
            break

        page_num += 1

    _sync_log(f"✅ Depositphotos: {total_saved} new records saved")


def _getty_api_collect_global(pw_page, force=False):
    """Збирає Getty/iStock через ESP stats API + завантажує TSV виписки."""
    from datetime import date as _date
    proc_file = os.path.join(RECIPES_DIR, "_processed_dates.json")
    try:
        with open(proc_file) as _f:
            _pd = json.load(_f)
    except Exception:
        _pd = {}

    today = _date.today()
    this_month = today.strftime("%Y-%m")
    getty_auto_done = _pd.get("Getty_auto_month", "")

    esp_url = "https://esp.gettyimages.com/contribute/stats"
    if "esp.gettyimages.com" not in pw_page.url:
        pw_page.goto(esp_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

    # Сесія валідна лише якщо є ccw cookie. URL може бути будь-який — Getty не завжди редіректить.
    def _has_ccw():
        try:
            # Get ALL cookies, then filter by name — ccw may be on any subdomain
            all_cookies = pw_page.context.cookies()
            return any(c["name"] == "ccw" for c in all_cookies)
        except Exception:
            return None  # browser closed
    state = _has_ccw()
    if state is None:
        _sync_log("⚠️ Getty/iStock: браузер закрито")
        return
    if not state:
        _sync_log("🔒 Getty/iStock: сесія протухла — залогінься у вікні (чекаю до 5 хв, нічого не закривай)")
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(3)
            state = _has_ccw()
            if state is None:
                _sync_log("⚠️ Getty/iStock: браузер закрито")
                return
            if state:
                _sync_log("✅ Getty/iStock: cookie отримано")
                # If user landed elsewhere after login, return to stats
                try:
                    if "/contribute/stats" not in pw_page.url:
                        pw_page.goto(esp_url, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(2)
                except Exception:
                    pass
                break
        else:
            _sync_log("⚠️ Getty/iStock: не дочекався логіну за 5 хв — пропускаю")
            return

    if not force:
        if today.day < 21:
            _sync_log(f"📅 Getty: залогінено ✓ — збір буде автоматично 21-го (сьогодні {today.day}-е)")
            return
        if getty_auto_done == this_month:
            _sync_log(f"📅 Getty: вже зібрано за {this_month} — нічого нового")
            return

    _sync_log("🚀 Getty/iStock: запускаю збір через ESP API...")

    def _get_sts_token():
        try:
            import urllib.parse as _up
            cookies = pw_page.context.cookies(["https://esp.gettyimages.com"])
            ccw_raw = next((c["value"] for c in cookies if c["name"] == "ccw"), "")
            if not ccw_raw:
                return ""
            b64 = _up.unquote(ccw_raw).split("|")[0]
            b64 += "=" * (4 - len(b64) % 4)
            return json.loads(base64.b64decode(b64))["sts_token"]
        except Exception as e:
            _sync_log(f"  ⚠️ sts_token: {e}")
            return ""

    sts_token = _get_sts_token()
    if not sts_token:
        _sync_log("🛑 Getty/iStock: не вдалося отримати sts_token")
        return
    _sync_log("🔑 Getty/iStock: sts_token отримано")

    def _fetch_page(from_date, to_date, page_n, sort="LastDownloadDate"):
        js = f"""async () => {{
            try {{
                const r = await fetch(
                    '/api/account/v1/statistics/downloads_for_search' +
                    '?orderResultsBy={sort}&sortDirection=Descending' +
                    '&page={page_n}&pageSize=50' +
                    '&fromDate={from_date}&toDate={to_date}' +
                    '&primaryDatePeriod=by_month',
                    {{credentials: 'include', headers: {{
                        accept: 'application/json',
                        Authorization: 'Bearer {sts_token}'
                    }}}}
                );
                if (!r.ok) return {{error: r.status}};
                return await r.json();
            }} catch(e) {{
                return {{error: e.toString()}};
            }}
        }}"""
        try:
            return pw_page.evaluate(js)
        except Exception as ex:
            return {"error": str(ex)}

    _sync_log("📥 Getty/iStock: переходжу на accountmanagement для завантаження виписок...")
    ACCT_URL = "https://accountmanagement.gettyimages.com/Reports/Export"
    CONTRACT_ID = "8368474:True"

    pw_page.goto(ACCT_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)

    if any(x in pw_page.url.lower() for x in ["sign-in", "login", "signin"]):
        _sync_log("🔒 Getty accountmanagement: потрібна авторизація — залогуйся в браузері що відкрився")
        try:
            pw_page.wait_for_url("**/Reports/**", timeout=120000)
        except Exception:
            _sync_log("⚠️ Getty: не дочекався логіну — пропускаю виписки")
            return
        time.sleep(2)

    avail_raw = pw_page.evaluate("""async () => {
        try {
            const r = await fetch('/Reports/AvailableStatementPeriod', {credentials: 'include'});
            return await r.json();
        } catch(e) { return {error: e.toString()}; }
    }""")

    if "error" in (avail_raw or {}) or not avail_raw:
        _sync_log(f"⚠️ Getty: не вдалось отримати список виписок: {avail_raw}")
        return

    raw_periods = (avail_raw.get("Options") or {}).get("AvailableStatementPeriods", [])
    periods = []
    for p_obj in raw_periods:
        val = p_obj.get("Value", "")
        if len(val) >= 7:
            periods.append((val[:4], val[5:7]))
    _sync_log(f"📋 Getty: знайдено {len(periods)} виписок")

    csrf_token = pw_page.evaluate("""() => {
        const el = document.querySelector('input[name="__RequestVerificationToken"]')
                 || document.querySelector('meta[name="csrf-token"]')
                 || document.querySelector('meta[name="__RequestVerificationToken"]');
        return el ? (el.value || el.getAttribute('content')) : null;
    }""")
    if not csrf_token:
        _sync_log("⚠️ Getty: CSRF токен не знайдено — спробую без нього")

    total_saved = total_skipped = 0
    import io as _io, csv as _csv

    def _import_tsv(content):
        nonlocal total_saved, total_skipped
        if not content or "Asset Number" not in content:
            return
        MONTH_MAP = {"jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
                     "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12"}
        def _parse_date(s):
            parts = s.strip().split("-")
            if len(parts) == 3:
                d, m, y = parts
                return f"{y}-{MONTH_MAP.get(m.lower(),'01')}-{d.zfill(2)} 00:00:00"
            return s
        reader = _csv.DictReader(_io.StringIO(content), delimiter="\t")
        with sqlite3.connect(DB_NAME, timeout=15) as _conn:
            for row in reader:
                asset_id = row.get("Asset Number", "").strip()
                filename = row.get("Alternate Asset Number", "").strip()
                title    = row.get("Asset Description", "").strip()
                coll     = row.get("Collection", "").strip()
                date_raw = row.get("Sales Date", "").strip()
                price_s  = row.get("Gross Royalty in USD", "0").strip()
                if not asset_id or not date_raw:
                    continue
                stock = "iStockphoto" if "premium" in coll.lower() else "iStock"
                price = float(price_s) if price_s else 0.0
                date  = _parse_date(date_raw)
                day   = date[:10]
                exists = _conn.execute(
                    'SELECT 1 FROM sales WHERE stock=? AND asset_id=? AND price=? AND date LIKE ?',
                    (stock, asset_id, price, day + '%')).fetchone()
                if exists:
                    total_skipped += 1
                    continue
                fname_no_ext = filename.rsplit(".", 1)[0] if filename and "." in filename else filename
                _conn.execute(
                    'INSERT INTO sales (asset_id,photo_name,stock,price,thumb_url,date,filename) VALUES (?,?,?,?,?,?,?)',
                    (asset_id, title or filename or asset_id, stock, price, None, date, fname_no_ext or ""))
                total_saved += 1
            _conn.commit()

    for year, month in periods:
        label = f"{year}-{month}"
        try:
            js_args = [year, month, CONTRACT_ID, csrf_token or ""]
            tsv = pw_page.evaluate("""async ([year, month, contract, csrf]) => {
                const body = new URLSearchParams();
                body.append('reportTypes', 'monthly');
                body.append('Years', year);
                body.append('Months', month);
                body.append('contractId', contract);
                body.append('exportFormat', 'tsv');
                if (csrf) body.append('__RequestVerificationToken', csrf);
                const r = await fetch('/Reports/Export', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: body.toString()
                });
                if (!r.ok) return null;
                return await r.text();
            }""", js_args)
            if not tsv or len(tsv.strip()) < 10:
                continue
            before = total_saved
            _import_tsv(tsv)
            added = total_saved - before
            if added:
                _sync_log(f"  📆 {label}: +{added} нових записів")
            time.sleep(0.3)
        except Exception as ex:
            _sync_log(f"  ⚠️ {label}: {ex}")

    _sync_log(f"✅ Getty/iStock TSV: +{total_saved} нових, {total_skipped} дублікатів")

    _sync_log("🖼️ Getty: оновлюю thumbnails через ESP API...")
    pw_page.goto("https://esp.gettyimages.com/contribute/stats",
                 wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)

    sts_token = _get_sts_token()
    if not sts_token:
        _sync_log("⚠️ Getty: sts_token недоступний після повернення на ESP")
        return

    import calendar as _cal
    now = datetime.now()
    thumb_updated = 0
    for offset in range(24):
        m_i = now.month - offset
        y_i = now.year
        while m_i <= 0:
            m_i += 12; y_i -= 1
        from_date = f"{y_i}-{m_i:02d}-01"
        last_d    = _cal.monthrange(y_i, m_i)[1]
        to_date   = f"{y_i}-{m_i:02d}-{last_d:02d}"
        if y_i == now.year and m_i == now.month:
            to_date = now.strftime("%Y-%m-%d")

        page_n = 1
        while True:
            data = _fetch_page(from_date, to_date, page_n)
            if "error" in data:
                break
            items = data.get("AssetDownloadSummaries", [])
            if not items:
                break
            for item in items:
                asset_id = str(item.get("MasterId", ""))
                thumb    = item.get("ThumbnailUrl", "")
                if asset_id and thumb:
                    with sqlite3.connect(DB_NAME, timeout=15) as _c:
                        _c.execute(
                            "UPDATE sales SET thumb_url=? WHERE asset_id=? "
                            "AND stock IN ('iStock','iStockphoto') AND (thumb_url IS NULL OR thumb_url='')",
                            (thumb, asset_id))
                        changed = _c.execute("SELECT changes()").fetchone()[0]
                    if changed:
                        thumb_updated += 1
                        load_img_async(asset_id, thumb, None)
            total_pages_g = (data.get("TotalAssetCount", 0) + 49) // 50
            if page_n >= total_pages_g:
                break
            page_n += 1

    _sync_log(f"✅ Getty/iStock: thumbs оновлено для {thumb_updated} активів")

    try:
        with open(proc_file) as _f:
            _pd2 = json.load(_f)
    except Exception:
        _pd2 = {}
    from datetime import date as _dt
    _pd2["Getty_auto_month"] = this_month
    _pd2["Getty/iStock_last_sync"] = _dt.today().isoformat()
    with open(proc_file, "w") as _f:
        json.dump(_pd2, _f, indent=2)
    _app_log(f"[Getty] last_sync recorded: {_pd2['Getty/iStock_last_sync']}")


def _run_collector_global(p, profile_dir, stock_name, start_url, headless):
    """Відкриває браузер і запускає потрібний колектор."""
    wait_cond = "networkidle" if stock_name == "Shutterstock" else "domcontentloaded"
    browser = _open_browser_context(p, profile_dir, headless)
    _apply_stealth(browser)
    pw_page = browser.pages[0] if browser.pages else browser.new_page()
    pw_page.goto(start_url, wait_until=wait_cond, timeout=60000)
    time.sleep(3 if stock_name != "Shutterstock" else 5)

    # Getty має свою login-логіку всередині колектора (чекає у тому самому вікні).
    # Не закриваємо браузер — просто йдемо далі.
    if _is_login_url(pw_page.url) and stock_name != "Getty Images":
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
    return browser, pw_page


def _collect_one_stock_global(name, url):
    """Збирає один сток у власному sync_playwright контексті."""
    from playwright.sync_api import sync_playwright as _spw
    import shutil
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
            # Getty: видимий тільки якщо сесія протухла (нема ccw cookie у профілі).
            # Якщо cookie валідна — синк у фоні (headless).
            if is_getty:
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
            browser, _ = _run_collector_global(_p, stock_profile, name, url, headless)
            _sync_log(f"[{name}] ✅ Done")
            browser.close()
    except Exception as ex:
        _sync_log(f"[{name}] 🛑 Error: {ex}")


def _sync_all_global():
    """Parallel collection from all stocks. Called from Flask /api/sync/start."""
    if _sync_all_active[0]:
        _sync_log("⚠️ Sync already running!")
        return
    _sync_all_active[0] = True
    _sync_stop_flag[0]  = False
    _sync_state["running"] = True
    _sync_state["log"]     = []
    _sync_log("🚀 Parallel sync from all stocks...")
    COLLECTABLE = {"Adobe Stock", "Shutterstock", "Getty Images", "Depositphotos"}
    try:
        threads = []
        for name, url in STOCK_URLS.items():
            if name not in COLLECTABLE:
                continue
            if _sync_stop_flag[0]:
                break
            t = threading.Thread(target=_collect_one_stock_global, args=(name, url), daemon=True)
            t.name = f"collector-{name}"
            t.start()
            threads.append((name, t))
        # Per-collector watchdog: max 10 min each. If a thread hangs (e.g. DataDome
        # blocks a fetch), we move on instead of locking the sync forever.
        WATCHDOG_SECONDS = 600
        for name, t in threads:
            t.join(timeout=WATCHDOG_SECONDS)
            if t.is_alive():
                _sync_log(f"[{name}] ⏱ watchdog timeout ({WATCHDOG_SECONDS}s) — moving on (browser may still be open)")
        if not _sync_stop_flag[0]:
            _sync_log("✅ All stocks collected")
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
        where  = ["stock != 'Envato Elements'"]
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
        where  = ["stock != 'Envato Elements'"]
        if period in cutoffs:
            where.append("date >= ?"); params.append(cutoffs[period])
        if stock != 'All':
            where.append("stock = ?"); params.append(stock)
        ws = ("WHERE " + " AND ".join(where)) if where else ""
        total_count = conn.execute(f"SELECT COUNT(*) FROM sales {ws}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, asset_id, price, thumb_url, date, stock "
            f"FROM sales {ws} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
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
                f" WHERE asset_id IN ({ph2}) AND stock != 'Envato Elements'"
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
        base_where  = "stock != 'Envato Elements'"
        base_params = []

    spans = {
        'today': (now.strftime('%Y-%m-%d'),
                  (now - timedelta(days=1)).strftime('%Y-%m-%d'),
                  (now - timedelta(days=1)).strftime('%Y-%m-%d')),
        'week':  ((now - timedelta(days=7)).strftime('%Y-%m-%d'), None,
                  (now - timedelta(days=14)).strftime('%Y-%m-%d')),
        'month': ((now - timedelta(days=30)).strftime('%Y-%m-%d'), None,
                  (now - timedelta(days=60)).strftime('%Y-%m-%d')),
        'year':  ((now - timedelta(days=365)).strftime('%Y-%m-%d'), None,
                  (now - timedelta(days=730)).strftime('%Y-%m-%d')),
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

        for key, (cur_from, cur_to, prev_from) in spans.items():
            if cur_to:
                # 'today' span: exact-day match. DB stores "YYYY-MM-DD HH:MM:SS",
                # so plain `date = '2026-05-21'` never matches — use LIKE with day prefix.
                cur_t, cur_c = q("date LIKE ?", [cur_from + '%'])
                prv_t, _     = q("date LIKE ?", [cur_to + '%'])
                ps = q_stock("date LIKE ?", [cur_from + '%'])
            else:
                cur_t, cur_c = q("date >= ?", [cur_from])
                prv_t, _     = q("date >= ? AND date < ?", [prev_from, cur_from])
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
            "SELECT stock, SUM(price), COUNT(*) FROM sales WHERE stock != 'Envato Elements' GROUP BY stock"
        ).fetchall()
        result['by_stock'] = [{"stock": r[0], "total": round(float(r[1] or 0), 2),
                                "count": r[2]} for r in by_stock]
    return jsonify(result)

@flask_app.route('/api/stock-list', methods=['GET'])
def api_stock_list():
    """Повертає список стоків (без Envato Elements)."""
    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        rows = conn.execute(
            "SELECT DISTINCT stock FROM sales WHERE stock != 'Envato Elements' AND stock IS NOT NULL ORDER BY stock"
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
                f" WHERE asset_id IN ({phs}) AND stock != 'Envato Elements'"
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
    lib = load_ms_library()

    # ── Build ms_library groups ──────────────────────────────────────────
    ms_groups: dict = {}   # gname → [{filename, thumb, stockids}]
    for photo in lib:
        gname = photo.get('group', '').strip()
        if not gname:
            continue
        ms_groups.setdefault(gname, []).append({
            'filename': photo.get('filename', ''),
            'ms_thumb': photo.get('thumb', ''),
            'stockids': {k: str(v) for k, v in photo.get('stockids', {}).items()
                         if k in _RELEVANT_STOCKS and v},
        })

    # ── Collect all IDs to query ─────────────────────────────────────────
    all_ids: set = set()
    for photos_raw in ms_groups.values():
        for ph in photos_raw:
            all_ids.update(ph['stockids'].values())
    user_groups = load_groups()
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


def _hash_based_matches(existing_matches, threshold=8):
    """
    Augment existing matches with perceptual-hash-based pairs.
    For all (stock, asset_id) in asset_meta, find visually similar across stocks
    (Hamming distance ≤ threshold) and merge into existing match groups.
    Returns merged matches dict + count of new pairs found.
    """
    with sqlite3.connect(DB_NAME, timeout=15) as c:
        rows = c.execute(
            "SELECT stock, asset_id, thumb_hash, aspect_ratio, r, g, b FROM asset_meta "
            "WHERE thumb_hash IS NOT NULL AND thumb_hash != ''"
        ).fetchall()
    if not rows:
        return existing_matches, 0

    # Index: aid -> (stock, hash, ar, r, g, b)
    by_aid = {aid: (stock, h, ar, r, g, b) for stock, aid, h, ar, r, g, b in rows}

    # Build aid -> set(group members) from existing matches for fast merging
    aid_to_group_key = {}
    for primary, members in existing_matches.items():
        for m in members:
            aid_to_group_key[m] = primary

    new_pairs = 0
    aids = list(by_aid.keys())
    # O(n^2) is OK for ≤ ~10k assets. For larger, would need LSH.
    for i, aid_a in enumerate(aids):
        sa, ha, ara, ra, ga, ba = by_aid[aid_a]
        for aid_b in aids[i+1:]:
            sb, hb, arb, rb, gb, bb = by_aid[aid_b]
            if sa == sb:
                continue  # same stock, can't be cross-stock match
            if ara and arb and abs(ara - arb) > 0.15:
                continue  # aspect ratios too different
            if _hamming_hex(ha, hb) > threshold:
                continue
            # Dominant-color check: total RGB distance must be ≤ 90 (≈30 per channel)
            try:
                if (isinstance(ra, int) and isinstance(rb, int) and
                    isinstance(ga, int) and isinstance(gb, int) and
                    isinstance(ba, int) and isinstance(bb, int) and
                    (abs(ra - rb) + abs(ga - gb) + abs(ba - bb)) > 90):
                    continue
            except Exception:
                pass  # Don't let bad data block matching
            # Found a visual match. Merge groups.
            ga = aid_to_group_key.get(aid_a)
            gb = aid_to_group_key.get(aid_b)
            if ga and gb:
                if ga == gb: continue
                # merge gb into ga
                existing_matches[ga] = sorted(set(existing_matches.get(ga, []) + existing_matches.get(gb, [])))
                for m in existing_matches.get(gb, []):
                    aid_to_group_key[m] = ga
                existing_matches.pop(gb, None)
            elif ga:
                existing_matches[ga] = sorted(set(existing_matches[ga] + [aid_b]))
                aid_to_group_key[aid_b] = ga
            elif gb:
                existing_matches[gb] = sorted(set(existing_matches[gb] + [aid_a]))
                aid_to_group_key[aid_a] = gb
            else:
                existing_matches[aid_a] = sorted([aid_a, aid_b])
                aid_to_group_key[aid_a] = aid_a
                aid_to_group_key[aid_b] = aid_a
            new_pairs += 1
    return existing_matches, new_pairs


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


def _ms_fname_to_libentry(disk_base, lib_filenames):
    """
    Map disk filename (without .jpg, e.g. 'B94A7017_45908268') to its base
    ms_library filename by stripping common variant suffixes.
    Returns the base name if found in lib_filenames, else None.
    """
    if disk_base in lib_filenames:
        return disk_base
    # _<digits> suffix (stock ID variants like _45908268)
    if '_' in disk_base:
        base = disk_base.rsplit('_', 1)[0]
        if base in lib_filenames:
            return base
    # -<word> suffix (e.g. -flipHorizontal, -bw, -1, -2)
    if '-' in disk_base:
        base = disk_base.rsplit('-', 1)[0]
        if base in lib_filenames:
            return base
    return None


def _ms_visual_matches(aid_to_group, strict_hamming=8, loose_hamming=12, loose_rgb_max=30):
    """
    Two-tier MS+ visual matching for sales aids without a group:
      - strict tier:  hamming ≤ strict_hamming   + RGB distance ≤ 90
      - loose tier:   hamming ≤ loose_hamming    + RGB distance ≤ loose_rgb_max
                      (catches near-duplicates from same shoot with slightly
                       different crop/edit — only accepted when colors are
                       near-identical, ruling out false positives)

    aid_to_group: existing {aid -> gname} map (mutated for new assignments)
    Returns: {gname: [new_aids...]} additions to apply to photo_groups.
    """
    lib = load_ms_library()
    # Build base-filename -> group map
    fname_to_group = {}
    for p in lib:
        fn = (p.get('filename') or '').strip()
        gn = (p.get('group') or '').strip()
        if fn and gn:
            fname_to_group[fn] = gn
    if not fname_to_group:
        return {}

    lib_filenames = set(fname_to_group.keys())

    with sqlite3.connect(DB_NAME, timeout=15) as c:
        sales_rows = c.execute(
            "SELECT stock, asset_id, thumb_hash, aspect_ratio, r, g, b FROM asset_meta "
            "WHERE thumb_hash IS NOT NULL AND thumb_hash != ''"
        ).fetchall()
        ms_rows = c.execute(
            "SELECT fname, thumb_hash, aspect_ratio, r, g, b FROM ms_meta "
            "WHERE thumb_hash IS NOT NULL AND thumb_hash != ''"
        ).fetchall()

    if not ms_rows:
        return {}

    # Resolve each disk fname → group (skip files whose base isn't in ms_library)
    ms_resolved = []
    for fname, h, ar, r, g, b in ms_rows:
        base = _ms_fname_to_libentry(fname, lib_filenames)
        if not base:
            continue
        gn = fname_to_group.get(base)
        if not gn:
            continue
        ms_resolved.append((gn, h, int(h, 16), ar, r, g, b))

    additions = {}
    for stock, aid, ha, ara, ra, ga, ba in sales_rows:
        aid = str(aid)
        if aid in aid_to_group:
            continue  # already grouped — skip
        try:
            ha_int = int(ha, 16)
        except Exception:
            continue
        best = None  # (hamming, gname)
        for gn, hb, hb_int, arb, rb, gb, bb in ms_resolved:
            if ara and arb and abs(ara - arb) > 0.15:
                continue
            # fast hamming via xor + popcount
            d = bin(ha_int ^ hb_int).count('1')
            if d > loose_hamming:
                continue
            try:
                if not (isinstance(ra, int) and isinstance(rb, int) and
                        isinstance(ga, int) and isinstance(gb, int) and
                        isinstance(ba, int) and isinstance(bb, int)):
                    rgb_dist = None
                else:
                    rgb_dist = abs(ra - rb) + abs(ga - gb) + abs(ba - bb)
            except Exception:
                rgb_dist = None
            # Tiered acceptance:
            if d <= strict_hamming:
                if rgb_dist is not None and rgb_dist > 90:
                    continue
            else:
                # loose tier — only accept if colors are near-identical
                if rgb_dist is None or rgb_dist > loose_rgb_max:
                    continue
            if best is None or d < best[0]:
                best = (d, gn)
                if d == 0:
                    break  # perfect match, no need to keep looking
        if best:
            gn = best[1]
            additions.setdefault(gn, []).append(aid)
            aid_to_group[aid] = gn
    return additions


@flask_app.route('/api/rebuild-matches', methods=['POST'])
def api_rebuild_matches():
    """Rebuild cross-stock matches + sync ms_library groups into photo_groups.json."""
    lib = load_ms_library()

    # 1. Rebuild _cross_stock_matches.json
    _PRIMARY_PRIORITY = ['adobestock', 'shutterstock', 'istock', 'esp', 'depositphotos']

    def _pick_primary(stockids):
        for key in _PRIMARY_PRIORITY:
            v = stockids.get(key)
            if v:
                return str(v)
        return None

    matches: dict = {}
    for photo in lib:
        stockids = photo.get('stockids', {})
        ids = [str(v) for k, v in stockids.items() if k in _RELEVANT_STOCKS and v]
        if len(ids) < 2:
            continue
        primary = _pick_primary(stockids)
        if not primary:
            continue
        if primary not in matches:
            matches[primary] = ids
        else:
            existing = set(matches[primary])
            existing.update(ids)
            matches[primary] = sorted(existing)

    # 1b. Augment with perceptual-hash matches (catches photos not in ms_library)
    matches, new_pairs = _hash_based_matches(matches)
    _save_matches(matches)

    # 2. Sync ms_library groups → photo_groups.json
    #    Only the PRIMARY stockid per photo — siblings are resolved via ms_library
    #    cross-stock lookup in api_groups_get, so adding them here causes duplicates.
    groups = load_groups()
    photos_synced = 0
    for photo in lib:
        gname = photo.get('group', '').strip()
        if not gname:
            continue
        stockids = photo.get('stockids', {})
        ids = [str(v) for k, v in stockids.items() if k in _RELEVANT_STOCKS and v]
        if not ids:
            continue
        primary = _pick_primary(stockids)
        if not primary:
            continue
        if gname not in groups:
            groups[gname] = []
        if primary not in groups[gname]:
            groups[gname].append(primary)
            photos_synced += 1

    # 3. Auto-propagate group membership to visually-matched siblings.
    #    If asset X is in group "Sunset" and asset Y has identical/near thumb_hash
    #    (via the `matches` dict augmented with hash pairs), add Y to the same group.
    #    Skips siblings already assigned to any other group (no overwrite).
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
            if not mkey:
                continue
            for sib in matches.get(mkey, []):
                if sib == aid or sib in aid_to_group:
                    continue
                groups[gname].append(sib)
                aid_to_group[sib] = gname
                auto_added += 1

    # 3b. MS+ visual matching: for each ungrouped sales aid, find the closest
    #     reference thumbnail in img_cache_ms/ and inherit its ms_library group.
    #     Closes the gap for sales photos that don't have an iStock/Adobe ID in
    #     ms_library.json but DO have a matching MS+ reference file.
    ms_added = 0
    ms_additions = _ms_visual_matches(aid_to_group)
    for gname, aids in ms_additions.items():
        if gname not in groups:
            groups[gname] = []
        groups[gname].extend(aids)
        ms_added += len(aids)

    # 4. Dedup all group lists (remove any sibling IDs added by previous rebuild runs)
    for gname in list(groups.keys()):
        seen: set = set()
        deduped = []
        for aid in groups[gname]:
            if aid not in seen:
                seen.add(aid)
                deduped.append(aid)
        groups[gname] = deduped
    save_groups(groups)

    return jsonify({'status': 'ok', 'entries': len(matches), 'photos': photos_synced,
                    'groups': len(groups), 'hash_pairs': new_pairs,
                    'auto_grouped': auto_added, 'ms_visual_grouped': ms_added})

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
    if stock and stock in STOCK_URLS:
        url = STOCK_URLS[stock]
        threading.Thread(target=_collect_one_stock_global, args=(stock, url), daemon=True).start()
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

@flask_app.route('/api/inspector/start', methods=['POST'])
def api_inspector_start():
    if _inspector_state["running"]:
        return jsonify({"ok": False, "msg": "already running"})
    data  = request.get_json(force=True, silent=True) or {}
    stock = data.get("stock", "Depositphotos")
    url   = STOCK_URLS.get(stock, "https://depositphotos.com/account/sales-history.html")
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
def _dhash_from_path(path):
    """
    Compute 64-bit dHash + dominant RGB from an image file.
    Returns (hex_string, aspect_ratio, r, g, b) or (None, None, None, None, None).
    """
    try:
        img = Image.open(path)
        ar = round(img.width / img.height, 3) if img.height else 1.0
        # Single resize → use for both hash (grayscale) and RGB (color)
        rgb_small = img.convert('RGB').resize((9, 8), Image.Resampling.LANCZOS)
        gray_px = list(rgb_small.convert('L').getdata())
        rgb_px  = list(rgb_small.getdata())
        bits = 0
        for row in range(8):
            base = row * 9
            for col in range(8):
                bits = (bits << 1) | (1 if gray_px[base + col] > gray_px[base + col + 1] else 0)
        n = len(rgb_px)
        r_avg = sum(p[0] for p in rgb_px) // n
        g_avg = sum(p[1] for p in rgb_px) // n
        b_avg = sum(p[2] for p in rgb_px) // n
        return f"{bits:016x}", ar, r_avg, g_avg, b_avg
    except Exception:
        return None, None, None, None, None

def _save_asset_meta(stock, asset_id, path):
    """Compute hash + dominant RGB for thumbnail at path and persist to asset_meta."""
    h, ar, r, g, b = _dhash_from_path(path)
    if not h: return
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.execute("""INSERT INTO asset_meta (stock, asset_id, thumb_hash, aspect_ratio, r, g, b, updated_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                         ON CONFLICT(stock, asset_id) DO UPDATE SET
                           thumb_hash=excluded.thumb_hash, aspect_ratio=excluded.aspect_ratio,
                           r=excluded.r, g=excluded.g, b=excluded.b,
                           updated_at=excluded.updated_at""",
                      (stock or '', str(asset_id), h, ar, r, g, b, datetime.now().isoformat()))
            c.commit()
    except Exception:
        pass

def _hamming_hex(h1, h2):
    """Hamming distance between two 16-char hex strings. 0 = identical, 64 = totally different."""
    try:
        return bin(int(h1, 16) ^ int(h2, 16)).count('1')
    except Exception:
        return 64
# ─────────────────────────────────────────────────────────────────────────────

def load_img(asset_id, url, stock=None):
    path = os.path.join(CACHE_DIR, f"{asset_id}.jpg")
    if os.path.exists(path): return path
    if not url: return None
    candidates = []
    if "stock.adobe.com" in url or "_F_" in url:
        candidates = [
            re.sub(r'\d{3}_F_', '500_F_', url),
            re.sub(r'\d{3}_F_', '360_F_', url),
        ]
    candidates.append(url)
    for u in candidates:
        if not u: continue
        try:
            r = req_lib.get(u, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                img = Image.open(BytesIO(r.content)).convert("RGB")
                img = ImageOps.fit(img, (400, 400), Image.Resampling.LANCZOS)
                img.save(path, "JPEG", quality=88)
                if stock:
                    _save_asset_meta(stock, asset_id, path)
                return path
        except Exception: continue
    return None

from concurrent.futures import ThreadPoolExecutor as _TPE
_img_executor  = _TPE(max_workers=5)
_http_executor = _TPE(max_workers=8)
atexit.register(lambda: (_img_executor.shutdown(wait=False), _http_executor.shutdown(wait=False)))

def load_match_thumb(asset_id, thumb_url):
    """Завантажує clean Adobe thumbnail у MATCH_CACHE_DIR, square crop 200×200."""
    path = os.path.join(MATCH_CACHE_DIR, f"{asset_id}.jpg")
    if os.path.exists(path): return path
    clean_url = _adobe_clean_thumb_url(thumb_url)
    if not clean_url: return None
    try:
        r = req_lib.get(clean_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            img = Image.open(BytesIO(r.content)).convert("RGB")
            img = ImageOps.fit(img, (200, 200), Image.Resampling.LANCZOS)
            img.save(path, "JPEG", quality=85)
            return path
    except Exception:
        pass
    return None

def load_img_async(asset_id, url, callback, is_adobe=False):
    def run():
        path = load_img(asset_id, url)
        if is_adobe and url:
            _img_executor.submit(lambda: load_match_thumb(asset_id, url))
        if path and callback: callback(path)
    try:
        _img_executor.submit(run)
    except Exception:
        pass

def load_groups() -> dict:
    try:
        with open(GROUPS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_groups(groups: dict):
    # Deduplicate: remove duplicate IDs within each group
    clean = {name: list(dict.fromkeys(str(a) for a in ids))
             for name, ids in groups.items() if ids}
    with open(GROUPS_FILE, "w") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)

_ms_library_cache: list = []
_ms_library_mtime: float = 0.0

def load_ms_library() -> list:
    global _ms_library_cache, _ms_library_mtime
    try:
        mtime = os.path.getmtime(MS_LIBRARY_FILE)
        if mtime == _ms_library_mtime and _ms_library_cache:
            return _ms_library_cache
        with open(MS_LIBRARY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _ms_library_cache = data if isinstance(data, list) else data.get("photos", [])
        _ms_library_mtime = mtime
        return _ms_library_cache
    except Exception:
        return []

def save_ms_library(photos: list):
    global _ms_library_cache, _ms_library_mtime
    seen = set(); deduped = []
    for p in photos:
        k = (p.get("directory",""), p.get("filename",""))
        if k not in seen:
            seen.add(k); deduped.append(p)
    with open(MS_LIBRARY_FILE, "w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False)
    _ms_library_cache = deduped
    _ms_library_mtime = os.path.getmtime(MS_LIBRARY_FILE)

# ═══════════════════════════════════════════════════════════
# TAB 0 — FEED (стрічка продажів)
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    print(f"🚀 Flask запущено на http://127.0.0.1:{FLASK_PORT}")
    flask_app.run(host='0.0.0.0', port=FLASK_PORT, use_reloader=False, threaded=True)