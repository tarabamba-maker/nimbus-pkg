"""
collectors/envato.py — Envato Elements earnings collector.

Strategy:
  - item_performance API on author.envato.com → earnings per item (period-based)
  - Snapshot-diff: compare with _envato_snapshot.json → save delta as sale record
  - Thumbnails: fetch() from elements.envato.com context (DataDome allows XHR)
  - Matching: Envato filename → MS+ basepath → clean thumb + group assignment

Auth: cookie-based via chrome_profile_Envato/ persistent profile.
CSRF token: read from <meta name="csrf-token"> on author.envato.com page.
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta

from app_globals import DB_NAME, RECIPES_DIR
from collectors.browser import _is_login_url
from image_utils import load_img_async
from sync_state import _app_log, _save_record, _sync_log, _sync_stop_flag

_SNAPSHOT_FILE = os.path.join(RECIPES_DIR, "_envato_snapshot.json")
_AUTHOR_BASE   = "https://author.envato.com"
_ELEMENTS_BASE = "https://elements.envato.com"


def _load_snapshot() -> dict:
    try:
        with open(_SNAPSHOT_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_snapshot(snap: dict):
    os.makedirs(RECIPES_DIR, exist_ok=True)
    with open(_SNAPSHOT_FILE, "w") as f:
        json.dump(snap, f, indent=2)


def _envato_collect(pw_page) -> bool:
    """Collect Envato Elements earnings via item_performance API + snapshot-diff."""

    # ── Step 1: navigate to author dashboard, get CSRF token ─────────────
    _sync_log("📊 Envato: loading author dashboard…")
    try:
        pw_page.goto(f"{_AUTHOR_BASE}/reports/performance",
                     wait_until="domcontentloaded", timeout=30000)
    except Exception as ex:
        _sync_log(f"⚠️ Envato: navigation failed: {ex}")
        return False

    if _is_login_url(pw_page.url) or "sign_in" in pw_page.url or "login" in pw_page.url:
        _sync_log("⚠️ Envato: not logged in — відкрий Browser tab → Envato → Login")
        return False

    # Extract CSRF token from page meta tag
    try:
        csrf = pw_page.locator('meta[name="csrf-token"]').get_attribute("content", timeout=5000)
    except Exception:
        csrf = ""
    if not csrf:
        _sync_log("⚠️ Envato: no CSRF token — спробуй перезайти")
        return False
    _sync_log(f"✅ Envato: CSRF ok, збираємо items…")

    # ── Step 2: fetch all items from item_performance API ─────────────────
    # Use page.evaluate to make browser-context fetch (DataDome-safe)
    def _get_performance_page(page_n: int, start: str, end: str) -> dict:
        js = f"""async () => {{
            const r = await fetch(
                "{_AUTHOR_BASE}/reports/api/v1/performance/item_performance" +
                "?shopfront=Elements&start_date={start}&end_date={end}" +
                "&sort_by=total_earnings&sort_direction=desc" +
                "&new_item_only=false&page={page_n}",
                {{
                    credentials: "include",
                    headers: {{
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "X-CSRF-Token": "{csrf}"
                    }}
                }}
            );
            if (!r.ok) return {{error: r.status}};
            return await r.json();
        }}"""
        try:
            return pw_page.evaluate(js)
        except Exception as ex:
            return {"error": str(ex)}

    # Determine date range: from last snapshot date to today
    # If no snapshot → use all-time (earliest possible date)
    prev_snapshot = _load_snapshot()
    is_first_sync = not bool(prev_snapshot)
    today = datetime.now().strftime("%Y-%m-%d")

    if is_first_sync:
        # All-time: use first_record_date from earnings/total API
        try:
            total_js = """async () => {
                const r = await fetch(
                    "https://author.envato.com/reports/api/v1/earnings/total",
                    {credentials: "include", headers: {"Accept": "application/json"}}
                );
                return await r.json();
            }"""
            total_data = pw_page.evaluate(total_js)
            start_date = total_data.get("data", {}).get("first_record_date", "2021-01-01")
        except Exception:
            start_date = "2021-01-01"
        _sync_log(f"📅 Envato: перший синк, all-time від {start_date}")
    else:
        # Incremental: from last sync date - 1 day
        try:
            last_dates = [v.get("last_sync_date", "") for v in prev_snapshot.values()
                          if isinstance(v, dict) and v.get("last_sync_date")]
            if last_dates:
                last_d = max(last_dates)
                start_date = (datetime.strptime(last_d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        except Exception:
            start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        _sync_log(f"📅 Envato: incremental від {start_date}")

    # Paginate through all items
    all_items = []
    page_n = 1
    while not _sync_stop_flag[0]:
        data = _get_performance_page(page_n, start_date, today)
        if "error" in data:
            _sync_log(f"⚠️ Envato: API error page {page_n}: {data['error']}")
            break
        items = data.get("data", [])
        if not items:
            break
        all_items.extend(items)
        total_pages = data.get("total_pages", 1)
        total_count = data.get("total_count", 0)
        _sync_log(f"   page {page_n}/{total_pages} — {len(all_items)}/{total_count} items")
        if page_n >= total_pages:
            break
        page_n += 1
        time.sleep(0.3)

    if not all_items:
        _sync_log("ℹ️ Envato: немає даних за цей period")
        return True

    _sync_log(f"✅ Envato: отримано {len(all_items)} items")

    # ── Step 3: snapshot-diff + save records ─────────────────────────────
    # Determine historical date for first sync (MIN(sales.date) - 1 day)
    if is_first_sync:
        try:
            with sqlite3.connect(DB_NAME, timeout=15) as c:
                r = c.execute("SELECT substr(MIN(date),1,10) FROM sales WHERE stock != 'Envato'").fetchone()
                first_db_date = r[0] if r and r[0] else "2020-01-01"
            hist_date = (datetime.strptime(first_db_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        except Exception:
            hist_date = "2020-01-01"
        _sync_log(f"   перший синк: earnings записуються як {hist_date}")
    else:
        hist_date = None  # not used on subsequent syncs

    new_snapshot = dict(prev_snapshot)  # copy, we'll update in place
    saved = 0
    skipped_negative = 0

    for item in all_items:
        if _sync_stop_flag[0]:
            break
        item_id       = str(item.get("item_id", ""))
        title         = item.get("title", "")
        filename      = item.get("filename", "")
        thumb_url     = item.get("thumb_url", "")  # filled in step 4
        new_total     = float(item.get("total_earnings", 0) or 0)
        category      = item.get("category", "photo")

        if not item_id:
            continue

        prev_entry = prev_snapshot.get(item_id)
        if isinstance(prev_entry, dict):
            prev_total = float(prev_entry.get("total", 0))
        elif isinstance(prev_entry, (int, float)):
            prev_total = float(prev_entry)
        else:
            prev_total = None  # new item

        delta = new_total - (prev_total or 0)

        # Always update snapshot (even on refund) — critical for correct future deltas
        new_snapshot[item_id] = {
            "total": new_total,
            "title": title,
            "filename": filename,
            "last_sync_date": today,
        }

        # Save to DB only if delta > 0
        if delta <= 0:
            if delta < 0:
                skipped_negative += 1
            continue

        # Date: historical for first-sync new items, today for incremental
        if prev_total is None and is_first_sync:
            record_date = hist_date
        else:
            record_date = today

        _save_record({
            "asset_id":   item_id,
            "photo_name": title,
            "stock":      "Envato",
            "price":      round(delta, 4),
            "date":       record_date,
            "thumb_url":  thumb_url,  # empty for now, filled in step 4
            "filename":   filename,
        })
        saved += 1

    _save_snapshot(new_snapshot)
    _sync_log(f"✅ Envato: збережено {saved} records"
              f"{f', пропущено {skipped_negative} refunds' if skipped_negative else ''}")

    # ── Step 4: fetch thumbnails for new/missing items ────────────────────
    # Navigate to elements.envato.com ONCE, then use fetch() — DataDome allows XHR
    items_needing_thumb = [
        item for item in all_items
        if item.get("item_id") and not _thumb_cached(str(item["item_id"]))
    ]

    if not items_needing_thumb:
        _sync_log("ℹ️ Envato: всі thumbnails вже закешовані")
        return True

    _sync_log(f"🖼️ Envato: завантажуємо {len(items_needing_thumb)} thumbnails…")

    try:
        pw_page.goto(_ELEMENTS_BASE, wait_until="domcontentloaded", timeout=20000)
        time.sleep(1.5)
    except Exception as ex:
        _sync_log(f"⚠️ Envato: не вдалося відкрити elements.envato.com: {ex}")
        return True  # earnings saved, thumbnails skip

    # Batch fetch thumbnails — 20 at a time with delays to avoid rate limiting
    BATCH = 20
    for i in range(0, len(items_needing_thumb), BATCH):
        if _sync_stop_flag[0]:
            break
        batch = items_needing_thumb[i:i + BATCH]
        ids = [str(it["item_id"]) for it in batch]

        js_batch = """async (ids) => {
            const results = {};
            for (const id of ids) {
                try {
                    const r = await fetch("/item-uuid-redirect/" + id, {
                        credentials: "include",
                        headers: {"Accept": "text/html"}
                    });
                    const html = await r.text();
                    const m = html.match(/og:image[^>]*content="([^"]+)"/);
                    results[id] = m ? m[1].replace(/&amp;/g, "&") : null;
                } catch(e) {
                    results[id] = null;
                }
                await new Promise(res => setTimeout(res, 250));
            }
            return results;
        }"""
        try:
            thumb_map = pw_page.evaluate(js_batch, ids)
        except Exception as ex:
            _sync_log(f"⚠️ Envato: thumbnail batch failed: {ex}")
            break

        for item in batch:
            item_id  = str(item["item_id"])
            thumb_url = thumb_map.get(item_id)
            if thumb_url:
                load_img_async(item_id, thumb_url, None, is_adobe=False, stock="Envato")

        fetched = sum(1 for v in thumb_map.values() if v)
        _sync_log(f"   thumbnails: {i + len(batch)}/{len(items_needing_thumb)} ({fetched} ok)")
        time.sleep(0.5)

    return True


def _thumb_cached(item_id: str) -> bool:
    """Check if thumbnail already cached in img_cache/."""
    from app_globals import CACHE_DIR
    return os.path.exists(os.path.join(CACHE_DIR, f"{item_id}.jpg"))
