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

    proc_file = os.path.join(RECIPES_DIR, "_processed_dates.json")
    try:
        with open(proc_file) as _f: _dp_proc = json.load(_f)
    except Exception:
        _dp_proc = {}
    def _save_dp_proc():
        try:
            with open(proc_file, "w") as _f: json.dump(_dp_proc, _f, indent=2)
        except Exception: pass

    def _fetch_rows(page_num):
        """Fetch one sales page → list of rows ([] = real empty, None = error)."""
        url = (f"/sales.html?limit=160&ajax=true" if page_num == 1
               else f"/sales/page{page_num}.html?limit=160&ajax=true")
        try:
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
                }} finally {{ clearTimeout(tid); }}
            }}""")
        except Exception as e:
            _sync_log(f"Depositphotos: fetch error page {page_num}: {e}")
            return None
        return extract_rows(html)

    def _save_row(row):
        # _save_record (not save_to_db) → new sale appended to _session_new_keys
        # for the blue "new" highlight.
        _save_record({"stock": "Depositphotos", "asset_id": row["asset_id"],
                      "price": row["price"], "date": row["date"],
                      "photo_name": row["title"], "thumb_url": row["thumb"]})
        load_img_async(row["asset_id"], row["thumb"], None, False, stock="Depositphotos")

    backfill_done = bool(_dp_proc.get("Depositphotos_backfill_done"))

    def _mark_backfill_done(why):
        _dp_proc["Depositphotos_backfill_done"] = True
        _dp_proc.pop("Depositphotos_backfill_page", None)
        _save_dp_proc()
        _sync_log(f"✅ Depositphotos: backfill complete ({why})")

    if not backfill_done:
        # ── Resumable historical backfill ──────────────────────────────────
        # ⚠️ Depositphotos REPEATS the last page forever past the end (never an
        # empty page). Stop EARLY — the moment we hit 6 consecutive already-saved
        # rows we've reached the collected/repeating tail → bail mid-page (don't
        # finish it). Resume from a persisted page cursor so an interrupted run
        # continues DEEPER next sync instead of re-walking from page 1.
        #   The 6-known-rows stop is ARMED only after we've seen ≥1 NEW row this
        #   run — otherwise resuming onto an already-collected page (cursor) would
        #   trip it immediately and never reach the deeper un-collected pages.
        page = max(1, int(_dp_proc.get("Depositphotos_backfill_page", 1)))
        prev_max = None
        stale_pages = 0
        consec_known = 0
        saw_new = False
        stop = False
        _sync_log(f"🔄 Depositphotos backfill: resume page {page}")
        while not _sync_stop_flag[0] and page <= 600 and not stop:
            rows = _fetch_rows(page)
            if rows is None:
                break                    # error → resume from cursor next sync
            if not rows:
                _mark_backfill_done("empty page"); break
            new = 0; pmax = ''
            for row in rows:
                if not row["date"]: continue
                if row["date"] > pmax: pmax = row["date"]
                if is_already_saved("Depositphotos", row["asset_id"], row["price"], row["date"]):
                    consec_known += 1
                    if saw_new and consec_known >= 6:
                        _mark_backfill_done("6 known rows — reached collected tail")
                        stop = True; break
                    continue
                consec_known = 0
                saw_new = True
                _save_row(row); new += 1; total_saved += 1
            _sync_log(f"Depositphotos backfill page {page}: +{new} (max {pmax})")
            if stop: break
            _dp_proc["Depositphotos_backfill_page"] = page
            _save_dp_proc()
            # Backstop for the already-fully-collected case (resume when everything
            # is done but the flag wasn't set): max date stops advancing on all-known
            # pages → DP is echoing the last page → done.
            if new == 0 and pmax and pmax == prev_max:
                stale_pages += 1
                if stale_pages >= 2:
                    _mark_backfill_done("page repeats — reached account start"); break
            else:
                stale_pages = 0
            prev_max = pmax
            page += 1
    else:
        # ── Fast incremental top-scan ──────────────────────────────────────
        # Rows are newest-first; once we hit 6 consecutive already-saved rows we've
        # reached collected territory → stop mid-page (don't scrape further pages).
        page = 1
        known_streak = 0
        stop = False
        while not _sync_stop_flag[0] and page <= 40 and not stop:
            rows = _fetch_rows(page)
            if not rows:
                break
            new = 0
            for row in rows:
                if not row["date"]: continue
                if is_already_saved("Depositphotos", row["asset_id"], row["price"], row["date"]):
                    known_streak += 1
                    if known_streak >= 6:
                        stop = True; break
                    continue
                known_streak = 0
                _save_row(row); new += 1; total_saved += 1
            _sync_log(f"Depositphotos: page {page} → +{new}")
            if stop:
                _sync_log("Depositphotos: 3 known rows in a row — caught up, stop")
            page += 1

    _sync_log(f"✅ Depositphotos: {total_saved} new records saved")
