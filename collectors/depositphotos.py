"""
collectors/depositphotos.py — Depositphotos collector.

Moved here from main.py (logic unchanged — only location changed):
  - _depositphotos_collect
"""

import sqlite3

from db import is_already_saved, save_to_db, DB_NAME
from sync_state import _sync_log, _sync_stop_flag
from collectors.browser import _is_login_url


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
            load_img_async(row["asset_id"], row["thumb"], None, False, stock="Depositphotos")
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
