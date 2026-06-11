"""
collectors/getty.py — Getty/iStock collectors.

Moved here from main.py (logic unchanged — only location changed):
  - _getty_collect_direct
  - _getty_api_collect_global
"""

import base64
import csv as _csv
import io as _io
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta

import requests as req_lib

from db import is_already_saved, DB_NAME
from sync_state import _sync_log, _sync_stop_flag, _save_record, _app_log
from cookies import _load_browser_cookies

_BASE_DIR = os.environ.get(
    "STOCK_DATA_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
RECIPES_DIR = os.path.join(_BASE_DIR, "recipes")


def _getty_collect_direct():
    """Getty/iStock via direct requests + in-app browser cookies. NO Playwright.
    Reads ccw + accountmanagement cookies from _pw_cookies.json, fetches CSRF
    token from /Reports/Export HTML, then POSTs to /Reports/Export per month
    to download TSV statements. Returns True on success."""
    from image_utils import load_img_async

    _sync_log("🚀 Getty direct: старт...")

    from datetime import date as _date_gt
    today = _date_gt.today()

    # Statement-availability gate: Getty publishes the statement for month M
    # around the 21st of month M+1. Skip ONLY when the newest statement that
    # could exist is already imported (in Getty_statements). If a previous
    # period is missing — always run, regardless of the day of month.
    proc_file_gt = os.path.join(RECIPES_DIR, "_processed_dates.json")
    try:
        with open(proc_file_gt) as _f: _pd_gt = json.load(_f)
    except Exception:
        _pd_gt = {}
    done_gt = set(_pd_gt.get("Getty_statements", []))
    if done_gt:
        # newest statement that should be available now
        y, m = today.year, today.month
        m -= 1 if today.day >= 21 else 2
        while m <= 0:
            m += 12; y -= 1
        expected = f"{y}-{m:02d}"
        if expected in done_gt:
            _sync_log(f"⏭️  Getty: останній доступний statement {expected} вже імпортовано — skip")
            return True

    try:
        all_cookies = _load_browser_cookies()
    except Exception as e:
        _sync_log(f"⚠️ Getty direct: cookies read failed: {e} — fallback")
        return False
    if not all_cookies:
        _sync_log("⚠️ Getty direct: no browser cookies — log in via the app browser")
        return False

    g_cookies = {c['name']: c['value'] for c in all_cookies
                 if 'gettyimages' in c.get('domain', '').lower()}
    if 'ccw' not in g_cookies:
        _sync_log("⚠️ Getty direct: no ccw cookie — log in via the app browser, fallback")
        return False

    import urllib.parse as _up
    session = req_lib.Session()
    session.cookies.update(g_cookies)
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                       "Version/17.5 Safari/605.1.15"),
    })

    # Step 1: GET /Reports/Export → CSRF + verify auth
    am_base = "https://accountmanagement.gettyimages.com"
    try:
        r = session.get(f"{am_base}/Reports/Export", timeout=20, allow_redirects=False)
    except Exception as e:
        _sync_log(f"⚠️ Getty direct: connect failed: {e} — fallback")
        return False
    if r.status_code != 200:
        _sync_log(f"⚠️ Getty direct: accountmanagement not logged in (HTTP {r.status_code}). "
                  f"Log in via the app browser → https://accountmanagement.gettyimages.com → fallback")
        return False
    csrf_match = re.search(r'name="__RequestVerificationToken"[^>]+value="([^"]+)"', r.text)
    if not csrf_match:
        _sync_log("⚠️ Getty direct: CSRF token not found in HTML — fallback")
        return False
    csrf = csrf_match.group(1)
    # Contract ID lives in HTML too; extract or use stored
    cid_match = re.search(r'(?:contractId|data-contract[\w-]*)["\s=:]+["\']?(\d+:True)', r.text)
    # Per-account contract is resolved properly from AvailableContracts below
    # (Step 2). The HTML regex is only a best-effort first guess.
    contract_id = cid_match.group(1) if cid_match else ""

    # Step 2: list available periods
    session.headers["Accept"] = "application/json"
    try:
        ar = session.get(f"{am_base}/Reports/AvailableStatementPeriod", timeout=20)
        avail = ar.json()
    except Exception as e:
        _sync_log(f"⚠️ Getty direct: AvailableStatementPeriod failed: {e} — fallback")
        return False
    _opts = avail.get("Options") or {}
    raw_periods = _opts.get("AvailableStatementPeriods", [])

    # Resolve THIS account's contract from the export form's contract dropdown
    # (AvailableContracts) — pick the selected one, else the first real contract.
    # Never hardcode: a baked-in contract id belongs to another account and makes
    # every statement export come back empty.
    contracts = _opts.get("AvailableContracts") or []
    sel = next((c for c in contracts if c.get("Selected") and c.get("Value")), None) \
        or next((c for c in contracts if c.get("Value")), None)
    if sel and sel.get("Value"):
        contract_id = sel["Value"]
    if not contract_id:
        _sync_log("⚠️ Getty direct: no contract found in AvailableContracts — fallback")
        return False
    _sync_log(f"🔑 Getty direct: contract={contract_id} ({(sel or {}).get('Text','')[:40]})")

    periods = []
    for p_obj in raw_periods:
        val = p_obj.get("Value", "")
        if len(val) >= 7:
            periods.append((val[:4], val[5:7]))
    _sync_log(f"📋 Getty direct: {len(periods)} statements available")

    # Early-stop: skip statements we've already fully imported.
    # Past months (NOT current month) don't change retroactively, so once
    # downloaded + parsed they're frozen. Re-fetching them wastes ~5-10 min
    # per sync. Current month always re-fetched (new sales may appear).
    proc_file = os.path.join(RECIPES_DIR, "_processed_dates.json")
    try:
        with open(proc_file) as _f: _pd = json.load(_f)
    except Exception:
        _pd = {}
    done_statements = set(_pd.get("Getty_statements", []))
    from datetime import date as _date_g
    cur_ym = _date_g.today().strftime("%Y-%m")
    skipped_periods = 0
    periods_to_fetch = []
    for year, month in periods:
        label = f"{year}-{month}"
        if label != cur_ym and label in done_statements:
            skipped_periods += 1
            continue
        periods_to_fetch.append((year, month))
    if skipped_periods:
        _sync_log(f"⏭️  Getty: пропущено {skipped_periods} уже оброблених statements")
    periods = periods_to_fetch

    # Step 3: parse + import TSV per period
    total_saved = total_skipped = 0
    MONTH_MAP = {"jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
                 "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12"}
    def _parse_d(s):
        parts = s.strip().split("-")
        if len(parts) == 3:
            d, m, y = parts
            return f"{y}-{MONTH_MAP.get(m.lower(),'01')}-{d.zfill(2)} 00:00:00"
        return s

    session.headers["Content-Type"] = "application/x-www-form-urlencoded"
    for year, month in periods:
        if _sync_stop_flag[0]:
            break
        label = f"{year}-{month}"
        body = (f"reportTypes=monthly&Years={year}&Months={month}"
                f"&contractId={_up.quote(contract_id)}&exportFormat=tsv"
                f"&__RequestVerificationToken={_up.quote(csrf)}")
        try:
            tr = session.post(f"{am_base}/Reports/Export", data=body, timeout=60)
        except Exception as e:
            _sync_log(f"  ⚠️ {label}: {e}")
            continue
        if tr.status_code != 200:
            _sync_log(f"  ⚠️ {label}: HTTP {tr.status_code}")
            continue
        content = tr.text
        if not content or "Asset Number" not in content:
            # No data for this period — do NOT mark it done, so a later sync (or
            # a fixed contract) retries it instead of freezing it empty forever.
            continue
        reader = _csv.DictReader(_io.StringIO(content), delimiter="\t")
        before = total_saved
        # Use higher timeout (60s) — under heavy parallel load other collectors
        # may hold the SQLite write lock; without retry, this whole month would
        # be skipped and ESP thumbs step would also never run.
        try:
            _conn = sqlite3.connect(DB_NAME, timeout=60)
        except Exception as e:
            _sync_log(f"  ⚠️ {label}: db open failed: {e}")
            continue
        try:
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
                date  = _parse_d(date_raw)
                day   = date[:10]
                exists = _conn.execute(
                    'SELECT 1 FROM sales WHERE stock=? AND asset_id=? AND price=? AND date LIKE ?',
                    (stock, asset_id, price, day + '%')).fetchone()
                if exists:
                    total_skipped += 1; continue
                fname_no_ext = filename.rsplit(".", 1)[0] if filename and "." in filename else filename
                _conn.execute(
                    'INSERT INTO sales (asset_id,photo_name,stock,price,thumb_url,date,filename) VALUES (?,?,?,?,?,?,?)',
                    (asset_id, title or filename or asset_id, stock, price, None, date, fname_no_ext or ""))
                total_saved += 1
            try: _conn.commit()
            except Exception as e: _sync_log(f"  ⚠️ {label}: commit failed: {e}")
        except Exception as e:
            _sync_log(f"  ⚠️ {label}: import error (continuing): {e}")
        finally:
            try: _conn.close()
            except Exception: pass
        added = total_saved - before
        if added:
            _sync_log(f"  📆 {label}: +{added} нових")
        # Mark statement as processed (skip on next sync if not current month).
        # Current month is intentionally NOT added so it stays refetched.
        if label != cur_ym:
            done_statements.add(label)
        time.sleep(0.3)

    _pd["Getty_statements"] = sorted(done_statements)
    try:
        with open(proc_file, "w") as _f: json.dump(_pd, _f, indent=2)
    except Exception: pass

    _sync_log(f"✅ Getty direct: +{total_saved} нових, {total_skipped} дублікатів")

    # Step 4: fetch ESP thumbnails and pair with stored asset_ids.
    # TSV gives sales rows without thumbs. ESP API
    # /api/account/v1/statistics/downloads_for_search returns MasterId + ThumbnailUrl
    # so we match by MasterId == asset_id and update sales.thumb_url.
    try:
        ccw_raw = g_cookies.get('ccw', '')
        b64 = _up.unquote(ccw_raw).split("|")[0]
        b64 += "=" * (4 - len(b64) % 4)
        sts_token = json.loads(base64.b64decode(b64))["sts_token"]
    except Exception as e:
        _sync_log(f"⚠️ Getty direct: sts_token: {e} — skipping thumbs")
        sts_token = None

    if sts_token:
        _sync_log("🖼️ Getty direct: тягну thumbnails з ESP API")
        esp_base = "https://esp.gettyimages.com"
        esp_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {sts_token}",
            "User-Agent": session.headers.get("User-Agent"),
        }
        import calendar as _cal
        from datetime import date as _date_g, datetime as _dt_g
        now_g = _dt_g.now()
        thumb_updated = 0
        for offset_m in range(60):   # up to 5 years of monthly thumb data
            if _sync_stop_flag[0]: break
            m_i = now_g.month - offset_m
            y_i = now_g.year
            while m_i <= 0:
                m_i += 12; y_i -= 1
            from_date = f"{y_i}-{m_i:02d}-01"
            last_d = _cal.monthrange(y_i, m_i)[1]
            to_date = f"{y_i}-{m_i:02d}-{last_d:02d}"
            if y_i == now_g.year and m_i == now_g.month:
                to_date = now_g.strftime("%Y-%m-%d")
            page_n = 1
            while True:
                url = (f"{esp_base}/api/account/v1/statistics/downloads_for_search"
                       f"?orderResultsBy=LastDownloadDate&sortDirection=Descending"
                       f"&page={page_n}&pageSize=50"
                       f"&fromDate={from_date}&toDate={to_date}"
                       f"&primaryDatePeriod=by_month")
                try:
                    r = req_lib.get(url, headers=esp_headers, cookies=g_cookies, timeout=20)
                    if r.status_code != 200:
                        break
                    data = r.json()
                except Exception as ex:
                    _sync_log(f"  ⚠️ ESP thumbs {y_i}-{m_i:02d} p{page_n}: {ex}")
                    break
                items = data.get("AssetDownloadSummaries", []) or []
                if not items:
                    break
                with sqlite3.connect(DB_NAME, timeout=15) as _c:
                    for item in items:
                        asset_id = str(item.get("MasterId", ""))
                        thumb = item.get("ThumbnailUrl", "")
                        if not asset_id or not thumb:
                            continue
                        _c.execute(
                            "UPDATE sales SET thumb_url=? WHERE asset_id=? "
                            "AND stock IN ('iStock','iStockphoto') AND (thumb_url IS NULL OR thumb_url='')",
                            (thumb, asset_id))
                        if _c.execute("SELECT changes()").fetchone()[0]:
                            thumb_updated += 1
                            load_img_async(asset_id, thumb, None, stock="iStock")
                    _c.commit()
                total_pages = (data.get("TotalAssetCount", 0) + 49) // 50
                if page_n >= total_pages:
                    break
                page_n += 1
                time.sleep(0.1)
        _sync_log(f"✅ Getty direct thumbs: оновлено для {thumb_updated} активів")

    # Mark Getty fully synced today — collector will skip until next month
    # (or until day-21 statement release if last run was earlier in same month).
    try:
        try:
            with open(proc_file_gt) as _f: pd = json.load(_f)
        except Exception:
            pd = {}
        from datetime import date as _date
        pd["Getty_auto_month"] = _date.today().strftime("%Y-%m")
        pd["Getty_last_run"]  = _date.today().strftime("%Y-%m-%d")
        with open(proc_file_gt, "w") as _f:
            json.dump(pd, _f, indent=2)
    except Exception:
        pass
    return True


def _getty_api_collect_global(pw_page, force=False):
    """Збирає Getty/iStock через ESP stats API + завантажує TSV виписки."""
    from image_utils import load_img_async

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
                        load_img_async(asset_id, thumb, None, stock="iStock")
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
