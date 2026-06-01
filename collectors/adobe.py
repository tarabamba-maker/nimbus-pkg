"""
collectors/adobe.py — Adobe Stock collectors.

Moved here from main.py (logic unchanged — only location changed):
  - _adobe_collect_direct
  - _adobe_api_collect_global
"""

import os
import json
import sqlite3
import time
from datetime import datetime, timedelta

import requests as req_lib

from db import is_already_saved, DB_NAME
from utils import _adobe_clean_thumb_url
from sync_state import _sync_log, _sync_stop_flag, _save_record
from cookies import _load_browser_cookies

_BASE_DIR   = os.environ.get("STOCK_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECIPES_DIR = os.path.join(_BASE_DIR, "recipes")


def _adobe_collect_direct():
    """Adobe Stock via direct requests + Safari cookies. NO Playwright.
    Calls contributor.stock.adobe.com/en/insights/sales-earnings with Adobe
    session cookies extracted from Safari. Returns True on success.

    ⚠️ DO NOT TOUCH — works at 100x speed. Headers `accept: application/json` +
    `x-requested-with: XMLHttpRequest` are mandatory (without them server
    returns HTML login page, not JSON)."""
    from image_utils import load_img_async
    _sync_log("🚀 Adobe direct: старт...")

    # Cross-platform: Safari on mac / Chrome on Windows. See _load_browser_cookies.
    try:
        all_cookies = _load_browser_cookies()
    except Exception as e:
        _sync_log(f"⚠️ Adobe direct: cookies read failed: {e} — fallback")
        return False
    if not all_cookies:
        _sync_log("⚠️ Adobe direct: no browser cookies — log in to Adobe in Safari (mac) / Chrome (win)")
        return False

    adobe_cookies = {c['name']: c['value'] for c in all_cookies
                     if 'adobe.com' in c.get('domain', '').lower()
                     or 'adobelogin.com' in c.get('domain', '').lower()}
    if 'RDC' not in adobe_cookies and 'ftauth_token' not in adobe_cookies and 'IMS' not in str(adobe_cookies):
        _sync_log(f"⚠️ Adobe direct: no session cookie (have {len(adobe_cookies)}) — fallback")
        return False
    _sync_log(f"🔑 Adobe direct: {len(adobe_cookies)} cookies")

    base = "https://contributor.stock.adobe.com"
    session = req_lib.Session()
    session.cookies.update(adobe_cookies)
    session.headers.update({
        "Accept": "application/json",
        "x-requested-with": "XMLHttpRequest",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                       "Version/17.5 Safari/605.1.15"),
    })

    def _get_page(path):
        try:
            r = session.get(base + path, timeout=20)
            if r.status_code != 200:
                return {"error": r.status_code}
            try:
                return r.json()
            except Exception:
                txt = r.text[:200]
                if any(x in txt.lower() for x in ['sign-in', 'login', 'ims-na1', 'adobelogin']):
                    return {"error": "not_json", "preview": txt, "needs_login": True}
                return {"error": "not_json", "preview": txt}
        except Exception as e:
            return {"error": str(e)}

    # Pass 1: recent sales (pages 1..N, no date filter)
    total_saved = 0
    page1 = _get_page(f"/en/insights/sales-earnings?limit=1000&page=1&pv={int(time.time()*1000)}")
    if "error" in page1:
        if page1.get('needs_login'):
            _sync_log("⚠️ Adobe direct: сесія expired в Safari — fallback to Playwright")
        else:
            _sync_log(f"⚠️ Adobe direct: page 1 error {page1.get('error')} — fallback")
        return False
    pagination = page1.get("view", {}).get("pagination", {})
    total_pages = pagination.get("pages", 1)
    total_items = pagination.get("total", 0)
    _sync_log(f"   → recent: {total_pages} стор., {total_items} записів")

    # ⚠️ CRITICAL: stop_pages early-exit MUST use `break`, not `return True`.
    # Past bug (v0.9.32→v0.9.41): `return True` killed the whole function,
    # silently skipping Pass 2 forever. Symptom: log shows "Adobe direct
    # recent: +N" then "done" without any "📅 Adobe direct: ..." chunk lines.
    stop_pages = False
    for pg in range(1, total_pages + 1):
        if _sync_stop_flag[0]: return True
        if stop_pages: break   # exit Pass 1 only — Pass 2 below still runs
        data = page1 if pg == 1 else _get_page(
            f"/en/insights/sales-earnings?limit=1000&page={pg}&pv={int(time.time()*1000)}")
        if "error" in data:
            _sync_log(f"  ⚠️ page={pg}: {data['error']}")
            continue
        history = data.get("sales", {}).get("history", [])
        page_old = 0; page_new = 0
        for item in history:
            asset_id = str(item.get("id", ""))
            price    = float(item.get("commissionAmount", 0))
            # Use the clean 110px preview (no watermark) for display, not the
            # watermarked thumbnailUrl Adobe returns by default.
            thumb    = _adobe_clean_thumb_url(item.get("thumbnailUrl", ""))
            sale_full = item.get("saleDate", "")
            if not asset_id or not sale_full[:10]: continue
            if is_already_saved("Adobe Stock", asset_id, price, sale_full):
                page_old += 1
                continue
            page_new += 1
            photo_name = item.get("title") or item.get("originalName") or asset_id
            orig = item.get("originalName") or ""
            fname = orig.rsplit(".", 1)[0] if orig and "." in orig else orig
            rec = {"asset_id": asset_id, "photo_name": photo_name, "stock": "Adobe Stock",
                   "price": price, "thumb_url": thumb, "date": sale_full, "filename": fname}
            if thumb:
                load_img_async(asset_id, thumb, None, is_adobe=True, stock="Adobe Stock")
            _save_record(rec)
            total_saved += 1
        if page_new == 0 and page_old >= 100:
            _sync_log(f"⏭️  Adobe: page {pg} all duplicates — stop")
            stop_pages = True
        if pg < total_pages:
            time.sleep(0.1)

    _sync_log(f"✅ Adobe direct recent: +{total_saved}")

    # Pass 2: historical chunks back 10 years.
    # Chunk size: 360 days (Adobe's documented max is 364; using 360 leaves
    # 4 days of safety margin in case Adobe tightens the limit).
    # Cuts Pass 2 from ~40 chunks (90d each) to ~10 chunks → ~10s faster sync.
    proc_file = os.path.join(RECIPES_DIR, "_processed_dates.json")
    try:
        with open(proc_file) as f:
            all_proc = json.load(f)
    except Exception:
        all_proc = {}
    adobe_done = set(all_proc.get("Adobe Stock", []))

    # First-sale guard: any chunk whose ENTIRE range falls before the user's
    # first known Adobe sale is unconditionally empty and never needs re-check.
    # For empty chunks in the post-first-sale window we DON'T mark them done →
    # they get re-checked next sync (cheap 1-request probe; protects against
    # API breakage that would otherwise silently lose history).
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as _c:
            _r = _c.execute(
                "SELECT substr(MIN(date),1,10) FROM sales WHERE stock='Adobe Stock'"
            ).fetchone()
            _first_sale_str = _r[0] if _r and _r[0] else None
        first_sale_d = (datetime.strptime(_first_sale_str, '%Y-%m-%d').date()
                        if _first_sale_str else None)
    except Exception:
        first_sale_d = None

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
        if _sync_stop_flag[0]: break
        chunk_start = max(chunk_end - timedelta(days=359), cutoff)
        months = _months_in_range(chunk_start, chunk_end)
        if months.issubset(adobe_done):
            chunk_end = chunk_start - timedelta(days=1); continue
        # Pre-first-sale chunk: skip entirely + mark all months done forever.
        if first_sale_d and chunk_end.date() < first_sale_d:
            adobe_done.update(months)
            all_proc["Adobe Stock"] = sorted(adobe_done)
            try:
                with open(proc_file, "w") as f: json.dump(all_proc, f, indent=2)
            except Exception: pass
            chunk_end = chunk_start - timedelta(days=1); continue
        s_str = chunk_start.strftime("%Y-%m-%d")
        e_str = chunk_end.strftime("%Y-%m-%d")
        _sync_log(f"📅 Adobe direct: {s_str} → {e_str}")

        ts = int(time.time() * 1000)
        d0 = _get_page(f"/en/insights/sales-earnings"
                      f"?end_date={e_str}&start_date={s_str}"
                      f"&time_range=day&timestamp={ts}&pv={ts}&limit=1000&page=1")
        if "error" in d0:
            _sync_log(f"  ⚠️ {d0['error']} — skip chunk")
            chunk_end = chunk_start - timedelta(days=1); continue

        range_pages = d0.get("view", {}).get("pagination", {}).get("pages", 1)
        range_total = d0.get("view", {}).get("pagination", {}).get("total", 0)
        _sync_log(f"   → {range_total} records, {range_pages} pages")

        chunk_new = 0
        for pg in range(1, range_pages + 1):
            if _sync_stop_flag[0]: break
            ts = int(time.time() * 1000)
            d = d0 if pg == 1 else _get_page(
                f"/en/insights/sales-earnings"
                f"?end_date={e_str}&start_date={s_str}"
                f"&time_range=day&timestamp={ts}&pv={ts}&limit=1000&page={pg}")
            if "error" in d:
                _sync_log(f"  ⚠️ page={pg}: {d['error']}"); continue
            for item in d.get("sales", {}).get("history", []):
                asset_id = str(item.get("id", ""))
                price    = float(item.get("commissionAmount", 0))
                thumb    = _adobe_clean_thumb_url(item.get("thumbnailUrl", ""))
                sale_full = item.get("saleDate", "")
                if not asset_id or not sale_full[:10]: continue
                if is_already_saved("Adobe Stock", asset_id, price, sale_full): continue
                photo_name = item.get("title") or item.get("originalName") or asset_id
                orig = item.get("originalName") or ""
                fname = orig.rsplit(".", 1)[0] if orig and "." in orig else orig
                rec = {"asset_id": asset_id, "photo_name": photo_name, "stock": "Adobe Stock",
                       "price": price, "thumb_url": thumb, "date": sale_full, "filename": fname}
                if thumb:
                    load_img_async(asset_id, thumb, None, is_adobe=True, stock="Adobe Stock")
                _save_record(rec)
                chunk_new += 1; hist_saved += 1
            if pg < range_pages: time.sleep(0.1)

        if chunk_new > 0:
            _sync_log(f"   ✚ {chunk_new} new")
        # Only mark months as "done" if Adobe actually returned records for this
        # chunk. Empty responses might be a temporary API breakage (history: 2021-2023
        # was lost this way once). Re-checking an empty chunk next sync costs 1 request.
        if range_total > 0:
            adobe_done.update(months)
            all_proc["Adobe Stock"] = sorted(adobe_done)
            try:
                with open(proc_file, "w") as f: json.dump(all_proc, f, indent=2)
            except Exception: pass
        chunk_end = chunk_start - timedelta(days=1)
        time.sleep(0.2)

    _sync_log(f"✅ Adobe direct ВСЬОГО: {total_saved + hist_saved}")
    return True


def _adobe_api_collect_global(pw_page):
    """Збирає Adobe Stock через API (browser fetch — обхід CSRF)."""
    from image_utils import load_img_async
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
                load_img_async(asset_id, thumb, None, is_adobe=True, stock="Adobe Stock")
            _save_record(rec)
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
                    load_img_async(asset_id, thumb, None, is_adobe=True, stock="Adobe Stock")
                _save_record(rec)
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
