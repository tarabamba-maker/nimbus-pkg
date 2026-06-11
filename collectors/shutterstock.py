"""
collectors/shutterstock.py — Shutterstock collectors.

Moved here from main.py (logic unchanged — only location changed):
  - _shutterstock_api_collect_direct
  - _shutterstock_api_collect_global
"""

import json
import os
import sqlite3
import time
from datetime import timedelta

import requests as req_lib

from db import is_already_saved, DB_NAME
from sync_state import _sync_log, _sync_stop_flag, _save_record
from cookies import _load_browser_cookies

_BASE_DIR = os.environ.get(
    "STOCK_DATA_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
RECIPES_DIR = os.path.join(_BASE_DIR, "recipes")


def _shutterstock_api_collect_direct():
    """Pure-requests Shutterstock collector. NO Playwright.
    Uses datadome trust cookie from the in-app browser login cache → bypasses
    anti-bot at full speed. Returns True on success.

    ⚠️ DO NOT TOUCH WITHOUT TESTING ON REAL SS ACCOUNT
    Hard requirements:
    1. session.cookies.update(ss_cookies) — passing cookies as kwarg per-request
       doesn't carry datadome properly across redirects.
    2. User-Agent must match what minted the datadome cookie. Generic UA → 403.
    3. x-end-app-name: contributor-web header — without it server returns HTML.
    4. consec_403 fallback — if datadome cookie expires mid-sync, fall back to
       Playwright so user can re-login.
    5. The 'accts_contributor' cookie check ensures the user is logged in.
    """
    from image_utils import load_img_async

    _sync_log("🚀 Shutterstock direct API: старт...")

    try:
        all_cookies = _load_browser_cookies()
    except PermissionError:
        _sync_log("⚠️ Shutterstock direct: Full Disk Access не наданий (mac) — fallback")
        return False
    except Exception as e:
        _sync_log(f"⚠️ Shutterstock direct: cookies read failed: {e} — fallback")
        return False
    if not all_cookies:
        _sync_log("⚠️ Shutterstock direct: no browser cookies — log in via the app browser")
        return False

    ss_cookies = {c['name']: c['value'] for c in all_cookies
                  if 'shutterstock.com' in c.get('domain', '')}
    if 'datadome' not in ss_cookies or 'accts_contributor' not in ss_cookies:
        _sync_log(f"⚠️ Shutterstock direct: required cookies missing "
                  f"(have {len(ss_cookies)}) — fallback")
        return False
    _sync_log(f"🔑 Shutterstock direct: {len(ss_cookies)} cookies (datadome ✓)")

    SS_CATEGORIES = [
        "single_image_and_other", "25_a_day", "on_demand", "enhanced",
        "footage_enhanced", "clip_packs", "unlimited_photo_commission",
        "unlimited_video_commission",
    ]
    base_url = "https://submit.shutterstock.com"
    session = req_lib.Session()
    session.cookies.update(ss_cookies)
    session.headers.update({
        "Accept": "application/json",
        "x-end-app-name": "contributor-web",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                       "Version/17.5 Safari/605.1.15"),
    })

    def _get(path):
        try:
            r = session.get(base_url + path, timeout=15)
            if r.status_code != 200:
                return {"error": r.status_code}
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    try:
        with sqlite3.connect(DB_NAME, timeout=15) as _c:
            _ss_count = _c.execute("SELECT COUNT(*) FROM sales WHERE stock='Shutterstock'").fetchone()[0]
    except Exception:
        _ss_count = 0

    from datetime import date as _date
    today = _date.today()

    # Recent/forward scan: always from MAX(date)-1 .. today (or last 45 days if empty).
    # Historical backfill is handled SEPARATELY below (resumable) so it can't be lost
    # if the long walk gets DataDome-throttled. The old "count==0 → full 2018 walk in
    # one shot" had no resume: a 403 abort left SS stuck on recent-only forever.
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as _c:
            _r = _c.execute(
                "SELECT substr(MAX(date),1,10) FROM sales WHERE stock='Shutterstock'"
            ).fetchone()
            _ss_max = _r[0] if _r and _r[0] else None
        _ss_from = _date.fromisoformat(_ss_max) - timedelta(days=1) if _ss_max else today - timedelta(days=45)
    except Exception:
        _ss_from = today - timedelta(days=45)
    scan_days = []
    d = today
    while d >= _ss_from:
        scan_days.append(d); d -= timedelta(days=1)
    _sync_log(f"⏩ Shutterstock direct: recent від {_ss_from} ({len(scan_days)} днів)")

    # Non-category keys in the aggregate day object (summaries/metadata) — never
    # query media_stats for these (would double-count or error).
    _AGG_DENYLIST = {"date", "total", "totals", "all", "summary", "earnings", "downloads"}
    _seen_cats = set(SS_CATEGORIES)

    agg_cache = {}
    def _day_cats(day):
        ym = (day.year, day.month)
        if ym not in agg_cache:
            agg = _get(f"/api/next/v2/earnings/aggregate?aggregation_period=day&year={ym[0]}&month={ym[1]}")
            agg_cache[ym] = {} if "error" in agg else \
                {d.get("date","")[:10]: d for d in agg.get("days", [])}
        info = agg_cache[ym].get(day.isoformat(), {})
        # DYNAMIC category discovery: collect EVERY key that's a per-category dict
        # with earnings>0 — not just a hardcoded whitelist. The old whitelist silently
        # dropped categories it didn't know (footage/video/spark_video) → missing $.
        cats = []
        for k, v in info.items():
            if k in _AGG_DENYLIST: continue
            if isinstance(v, dict) and v.get("earnings", 0) > 0:
                cats.append(k)
                if k not in _seen_cats:
                    _seen_cats.add(k)
                    _sync_log(f"  🆕 Shutterstock: нова категорія '{k}' (раніше ігнорувалась)")
        return cats

    # Per-day scanner shared by the recent scan AND the historical backfill.
    # Returns (day_new, day_already, hit_403_streak). On a 3× consecutive 403 the
    # caller must stop (DataDome blocked us) — but progress is already persisted by
    # the backfill cursor, so the next sync resumes where we left off.
    def _collect_day(date_str, active_cats, counter):
        day_new = 0; day_already = 0
        for cat in active_cats:
            page_n = 1
            while True:
                data = _get(f"/api/next/v2/earnings/media_stats/day"
                           f"?display_column={cat}&date={date_str}&page={page_n}&per_page=100")
                if "error" in data:
                    if data['error'] == 403:
                        counter[0] += 1
                        if counter[0] >= 3:
                            return day_new, day_already, True
                    else:
                        _sync_log(f"  ⚠️ {date_str}/{cat} p{page_n}: {data['error']}")
                    break
                counter[0] = 0
                items = data.get("media", [])
                if not items:
                    break
                for item in items:
                    asset_id = str(item.get("mediaId", ""))
                    price    = float(item.get("total", 0))
                    thumb    = (item.get("details") or {}).get("previewImageUrl", "")
                    name     = (item.get("details") or {}).get("description", "") or asset_id
                    if not asset_id or price == 0: continue
                    if is_already_saved("Shutterstock", asset_id, price, date_str):
                        day_already += 1; continue
                    rec = {"asset_id": asset_id, "stock": "Shutterstock",
                           "price": price, "thumb_url": thumb,
                           "photo_name": name, "date": date_str}
                    if thumb:
                        load_img_async(asset_id, thumb, None, stock="Shutterstock")
                    _save_record(rec)
                    day_new += 1
                if page_n >= data.get("pages", 1): break
                page_n += 1
                time.sleep(0.1)   # gentle throttle, well below trigger
        return day_new, day_already, False

    total_saved = 0
    stop_early = False
    consec = [0]
    for scan_day in scan_days:
        if stop_early or _sync_stop_flag[0]:
            break
        date_str = scan_day.isoformat()
        active_cats = _day_cats(scan_day)
        if not active_cats:
            continue
        day_new, day_already, blocked = _collect_day(date_str, active_cats, consec)
        total_saved += day_new
        if blocked:
            _sync_log("🛑 Shutterstock direct: 3× HTTP 403 — datadome cookie expired, fallback")
            return False
        if day_new > 0 and total_saved % 200 < day_new:
            _sync_log(f"  📆 {date_str}: +{day_new} (total {total_saved})")
        if _ss_count > 0 and day_new == 0 and day_already > 0:
            _sync_log(f"  ✅ {date_str}: caught up — stop")
            stop_early = True

    _sync_log(f"✅ Shutterstock direct API recent: {total_saved} нових записів")

    # ── Historical backfill (resumable across syncs) ──────────────────────────
    # Walk BELOW the earliest collected day toward account start. SS exposes every
    # month/year back to signup, but the long day-by-day walk gets DataDome-throttled,
    # so we persist a cursor (recipes/_processed_dates.json) and resume next sync until
    # the floor is reached. Without this, an interrupted cold-start left SS recent-only
    # forever (the 3809-vs-36847 gap) because incremental mode only scans FORWARD.
    SS_FLOOR = _date(2015, 1, 1)
    proc_file = os.path.join(RECIPES_DIR, "_processed_dates.json")
    try:
        with open(proc_file) as f: _ss_proc = json.load(f)
    except Exception:
        _ss_proc = {}

    def _save_proc():
        try:
            with open(proc_file, "w") as f: json.dump(_ss_proc, f, indent=2)
        except Exception: pass

    if not _ss_proc.get("Shutterstock_backfill_done"):
        cur = _ss_proc.get("Shutterstock_backfill_cursor")
        if cur:
            bday = _date.fromisoformat(cur) - timedelta(days=1)
        else:
            try:
                with sqlite3.connect(DB_NAME, timeout=15) as _c:
                    _r = _c.execute(
                        "SELECT substr(MIN(date),1,10) FROM sales WHERE stock='Shutterstock'"
                    ).fetchone()
                    _ss_min = _r[0] if _r and _r[0] else today.isoformat()
            except Exception:
                _ss_min = today.isoformat()
            bday = _date.fromisoformat(_ss_min) - timedelta(days=1)
        _sync_log(f"⏬ Shutterstock backfill: {bday} → {SS_FLOOR}")
        bf_saved = 0
        reached_floor = True
        while bday >= SS_FLOOR:
            if _sync_stop_flag[0]:
                reached_floor = False; break
            date_str = bday.isoformat()
            active_cats = _day_cats(bday)
            if active_cats:
                day_new, _da, blocked = _collect_day(date_str, active_cats, consec)
                bf_saved += day_new
                if day_new > 0 and bf_saved % 200 < day_new:
                    _sync_log(f"  ⏬ {date_str}: +{day_new} (backfill {bf_saved})")
                if blocked:
                    # Persist progress and bail — resume here next sync.
                    _ss_proc["Shutterstock_backfill_cursor"] = bday.isoformat()
                    _save_proc()
                    _sync_log(f"🛑 SS backfill: 403 — paused at {date_str}, resume next sync (+{bf_saved})")
                    return True
            # Persist the cursor EVERY day so closing the app mid-walk loses nothing
            # — on restart we resume from exactly here and never re-collect below it.
            # (tiny <1KB file; writes are spread across the per-day throttle sleep.)
            _ss_proc["Shutterstock_backfill_cursor"] = bday.isoformat()
            _save_proc()
            bday -= timedelta(days=1)
        if reached_floor:
            _ss_proc["Shutterstock_backfill_done"] = True
            _ss_proc["Shutterstock_backfill_cursor"] = SS_FLOOR.isoformat()
            _save_proc()
            _sync_log(f"✅ Shutterstock backfill complete (+{bf_saved})")

    return True


def _shutterstock_api_collect_global(pw_page):
    """Збирає Shutterstock через aggregate API + media_stats/day per-photo."""
    from image_utils import load_img_async

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

    import random as _rnd
    if _ss_count == 0:
        # Full history: 2018→today with HUMAN-LIKE pacing to avoid DataDome trip.
        # Randomized pauses + occasional long breaks mimic actual user browsing.
        # Estimated runtime ~4-8 hours for 7 years — perfect for overnight run.
        _sync_log(f"🔄 Shutterstock: БД пуста — обережний збір повної історії з 2018 (рандомні паузи)")
        scan_days = []
        d = _date(2018, 1, 1)
        while d <= today:
            scan_days.append(d)
            d += timedelta(days=1)
        recent_days = list(reversed(scan_days))
        PAGE_SLEEP_RANGE  = (2.5, 5.5)      # between paginated category pages
        DAY_SLEEP_RANGE   = (3.5, 9.0)      # between days
        HEAVY_PAUSE_EVERY = 25              # every N days
        HEAVY_PAUSE_RANGE = (45, 120)       # 45-120 sec break
        SLOW_MODE = True
    else:
        recent_days = [(today - timedelta(days=i)) for i in range(30)]
        _sync_log(f"⏩ Shutterstock: інкрементальний збір — останні 30 днів...")
        PAGE_SLEEP_RANGE  = (0.3, 0.8)
        DAY_SLEEP_RANGE   = (0.0, 0.3)
        HEAVY_PAUSE_EVERY = 9999
        HEAVY_PAUSE_RANGE = (0, 0)
        SLOW_MODE = False
    stop_early = False

    _agg_cache = {}

    _AGG_DENYLIST = {"date", "total", "totals", "all", "summary", "earnings", "downloads"}
    _seen_cats2 = set(SS_CATEGORIES)

    def _get_day_cats(day: _date):
        ym = (day.year, day.month)
        if ym not in _agg_cache:
            agg = _fetch(f"/api/next/v2/earnings/aggregate"
                         f"?aggregation_period=day&year={ym[0]}&month={ym[1]}")
            _agg_cache[ym] = {d.get("date","")[:10]: d
                              for d in agg.get("days", [])} if "error" not in agg else {}
        day_info = _agg_cache[ym].get(day.isoformat(), {})
        # Dynamic discovery (see direct collector) — never drop unknown categories.
        cats = []
        for k, v in day_info.items():
            if k in _AGG_DENYLIST: continue
            if isinstance(v, dict) and v.get("earnings", 0) > 0:
                cats.append(k)
                if k not in _seen_cats2:
                    _seen_cats2.add(k)
                    _sync_log(f"  🆕 Shutterstock: нова категорія '{k}'")
        return cats

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
                        load_img_async(asset_id, thumb, None, stock="Shutterstock")
                    _save_record(rec)
                    day_new    += 1
                    total_saved += 1

                if page_n >= data.get("pages", 1):
                    break
                page_n += 1
                time.sleep(_rnd.uniform(*PAGE_SLEEP_RANGE))

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
