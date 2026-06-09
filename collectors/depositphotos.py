"""
collectors/depositphotos.py — Depositphotos collector.

Moved here from main.py (logic unchanged — only location changed):
  - _depositphotos_collect
"""

import os
import json
import sqlite3

from db import is_already_saved, DB_NAME
from sync_state import _sync_log, _sync_stop_flag, _save_record
from collectors.browser import _is_login_url

_BASE_DIR   = os.environ.get("STOCK_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECIPES_DIR = os.path.join(_BASE_DIR, "recipes")


def _depositphotos_collect(pw_page):
    """Collect Depositphotos sales via HTML scraping of /sales/pageN.html?ajax=true"""
    from image_utils import load_img_async
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
            _last_r = _c.execute("SELECT substr(MAX(date),1,10) FROM sales WHERE stock='Depositphotos'").fetchone()
            last_known = (_last_r[0] or '') if _last_r else ''
    except Exception:
        _dp_count = 0
        last_known = ''
    # Resumable backfill state. Until the full history has been walked to its end
    # (an empty page) ONCE, we keep walking deep and DON'T early-stop at last_known —
    # otherwise an interrupted cold-start (app closed / DataDome) leaves the older
    # pages it never reached permanently un-collected, because the next sync's
    # last_known=MAX(date) stops it after page 1. Dedup makes the re-walk free of dups.
    proc_file = os.path.join(RECIPES_DIR, "_processed_dates.json")
    try:
        with open(proc_file) as _f: _dp_proc = json.load(_f)
    except Exception:
        _dp_proc = {}
    def _save_dp_proc():
        try:
            with open(proc_file, "w") as _f: json.dump(_dp_proc, _f, indent=2)
        except Exception: pass
    backfilling = not _dp_proc.get("Depositphotos_backfill_done")
    if backfilling:
        last_known = ''          # disable the early-stop so we reach the true end
        PAGE_CAP = 500
        _sync_log("🔄 Depositphotos: історичний backfill (до кінця, resumable)")
    else:
        PAGE_CAP = 30

    while not _sync_stop_flag[0]:
        # limit=160 returns 160 rows/page instead of default ~40 → 4x fewer requests
        url = (f"/sales.html?limit=160&ajax=true" if page_num == 1
               else f"/sales/page{page_num}.html?limit=160&ajax=true")
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
            if backfilling:
                _dp_proc["Depositphotos_backfill_done"] = True
                _save_dp_proc()
                _sync_log("✅ Depositphotos: backfill complete (reached account start)")
            break

        new_in_page = 0
        page_max_date = ''
        stop_after_page = False
        for row in rows:
            if not row["date"]:
                continue
            if row["date"] > page_max_date:
                page_max_date = row["date"]
            # Row-level early stop: if this row's date is older than last known,
            # every subsequent row is also older → no need to process further.
            if last_known and row["date"][:10] < last_known:
                stop_after_page = True
                break
            if is_already_saved("Depositphotos", row["asset_id"], row["price"], row["date"]):
                continue
            # _save_record (not save_to_db) so the new sale is appended to
            # _session_new_keys → client paints the blue "new" highlight.
            _save_record({"stock": "Depositphotos", "asset_id": row["asset_id"],
                          "price": row["price"], "date": row["date"],
                          "photo_name": row["title"], "thumb_url": row["thumb"]})
            load_img_async(row["asset_id"], row["thumb"], None, False, stock="Depositphotos")
            new_in_page  += 1
            total_saved  += 1

        _sync_log(f"Depositphotos: page {page_num} → {new_in_page} new (max date {page_max_date})")

        if stop_after_page:
            _sync_log("Depositphotos: reached older dates, stopping")
            break

        # Fallback stop conditions — ONLY in incremental mode. During backfill an
        # all-known page is expected (we may have collected it on an earlier partial
        # run) and we MUST keep going deeper to reach pages we never reached.
        if not backfilling:
            if new_in_page == 0:
                all_known_streak += 1
                if (all_known_streak >= 2
                        or (last_known and page_max_date and page_max_date <= last_known)):
                    _sync_log("Depositphotos: caught up to last known sale, stopping")
                    break
            else:
                all_known_streak = 0
        if page_num >= PAGE_CAP:
            _sync_log(f"Depositphotos: hit {PAGE_CAP}-page cap, stopping (resume next sync)")
            break

        page_num += 1

    _sync_log(f"✅ Depositphotos: {total_saved} new records saved")
