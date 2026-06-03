"""
collectors/envato.py — Envato Elements earnings collector.

DATA MODEL (confirmed empirically 2026-06-02 — see CLAUDE.md):
  Envato exposes earnings through two report APIs on author.envato.com:

  1. earnings/detail?view=monthly  → MONTHLY AGGREGATE totals (elements+market),
     available from `monthly_first_record_date` (2021-06). NO per-item breakdown.
  2. performance/item_performance  → PER-ITEM earnings, but only from
     `daily_first_record_date` (~2025-05). Before that it returns 0 items.

  So history splits at the daily boundary:
    • months  <  boundary  → one AGGREGATE record per month (asset_id "envato-YYYY-MM").
                              Envato has no per-photo data there ($16k of 2021-2024).
    • months  >= boundary  → real PER-ITEM records (Best Sellers / matching work here).

  Both endpoints scope total_earnings by start_date/end_date (verified:
  probe item 2025=$20.55 + 2026=$24.61 = all-time $45.16). So we walk month by
  month and date each record to its own month — proper Analytics timeline,
  no all-on-one-day dump.

INCREMENTAL: `recipes/_envato_months.json` stores {YYYY-MM: month_total} from the
  last sync. A month is (re)collected only when its total changed vs last sync —
  this catches Envato's 1-2 month settling delay without re-flashing unchanged
  months blue.

Auth: cookie-based via chrome_profile_Envato/. CSRF from <meta name="csrf-token">.
Thumbnails: only for per-item months — fetch() from elements.envato.com (DataDome
  blocks navigation but allows in-page XHR).
"""

import calendar
import json
import os
import sqlite3
import time
from datetime import date, datetime

from app_globals import DB_NAME, RECIPES_DIR
from collectors.browser import _is_login_url
from image_utils import load_img_async
from sync_state import _app_log, _save_record, _sync_log, _sync_stop_flag

_MONTHS_FILE    = os.path.join(RECIPES_DIR, "_envato_months.json")
_PORTFOLIO_FILE = os.path.join(RECIPES_DIR, "_envato_portfolio.json")
_AUTHOR_BASE    = "https://author.envato.com"
_PORTFOLIO_BASE = "https://portfolio.envato.com"


def _load_months() -> dict:
    try:
        with open(_MONTHS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_months(m: dict):
    os.makedirs(RECIPES_DIR, exist_ok=True)
    with open(_MONTHS_FILE, "w") as f:
        json.dump(m, f, indent=2)


def _month_iter(start_ym: str, end_ym: str):
    """Yield 'YYYY-MM' strings inclusive from start_ym to end_ym."""
    y, m = int(start_ym[:4]), int(start_ym[5:7])
    ey, em = int(end_ym[:4]), int(end_ym[5:7])
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            m = 1; y += 1


_YEAR_ACCENT = {2021: (38, 166, 154), 2022: (66, 133, 244), 2023: (149, 117, 205),
                2024: (255, 167, 38), 2025: (102, 187, 106), 2026: (236, 89, 89)}


def _card_font(sz):
    from PIL import ImageFont
    for p in ("/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "C:/Windows/Fonts/arialbd.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz, index=1 if p.endswith(".ttc") else 0)
            except Exception:
                pass
    return ImageFont.load_default()


def _make_month_card(ym: str):
    """Render a clean month placard for an aggregate `envato-YYYY-MM` record
    (these have no real photo). Per-year accent colour. Saved to img_cache."""
    from PIL import Image, ImageDraw
    from app_globals import CACHE_DIR
    try:
        y, m = int(ym[:4]), int(ym[5:7])
        acc = _YEAR_ACCENT.get(y, (120, 144, 156))
        img = Image.new("RGB", (400, 400), (24, 28, 36))
        d = ImageDraw.Draw(img)
        for i in range(400):
            t = i / 400
            d.line([(0, i), (400, i)], fill=(int(24 + 10 * t), int(28 + 12 * t), int(36 + 16 * t)))
        d.rectangle([0, 0, 8, 400], fill=acc)
        d.text((28, 30), "ENVATO ELEMENTS", font=_card_font(18), fill=(150, 160, 175))
        d.text((28, 150), calendar.month_name[m].upper(), font=_card_font(54), fill=(238, 242, 248))
        d.text((28, 220), str(y), font=_card_font(72), fill=acc)
        d.text((28, 340), "monthly earnings", font=_card_font(20), fill=(120, 132, 148))
        d.rounded_rectangle([300, 34, 360, 94], radius=8, outline=acc, width=3)
        d.line([(300, 52), (360, 52)], fill=acc, width=3)
        img.save(os.path.join(CACHE_DIR, f"envato-{ym}.jpg"), "JPEG", quality=90)
    except Exception as ex:
        _app_log(f"[Envato] month card {ym} failed: {ex}")


def _month_date(ym: str) -> str:
    """Date string for a month's records: last day of month, capped at today."""
    y, m = int(ym[:4]), int(ym[5:7])
    last = calendar.monthrange(y, m)[1]
    d = date(y, m, last)
    today = date.today()
    if d > today:
        d = today
    return d.strftime("%Y-%m-%d")


def _thumb_cached(item_id: str) -> bool:
    from app_globals import CACHE_DIR
    return os.path.exists(os.path.join(CACHE_DIR, f"{item_id}.jpg"))


def _fetch_json(pw_page, path: str, csrf: str):
    """Browser-context fetch of an author.envato.com JSON endpoint (DataDome-safe)."""
    js = f"""async () => {{
        const r = await fetch("{_AUTHOR_BASE}{path}", {{
            credentials: "include",
            headers: {{"Accept":"application/json","Content-Type":"application/json",
                       "X-CSRF-Token":"{csrf}"}}
        }});
        if (!r.ok) return {{__error: r.status}};
        return await r.json();
    }}"""
    try:
        return pw_page.evaluate(js)
    except Exception as ex:
        return {"__error": str(ex)}


def _envato_collect(pw_page):
    """Collect Envato Elements earnings. Returns True on success, 'needs_login' if
    the session expired (orchestrator then triggers the login flow)."""

    # ── Step 1: confirm session ───────────────────────────────────────────
    _sync_log("📊 Envato: перевіряємо сесію…")
    for _ in range(10):
        url = pw_page.url
        # strip "author." so the substring "auth" doesn't false-match (see browser.py)
        if not _is_login_url(url.replace("author.", "")):
            break
        time.sleep(0.5)
    else:
        _sync_log("⚠️ Envato: не залогінений — Import Cookies або кнопка Envato в Browser tab")
        return "needs_login"
    _sync_log(f"✅ Envato: сесія активна ({pw_page.url[:60]}…)")

    # CSRF token (retry — page may still be settling)
    csrf = ""
    for _ in range(15):
        try:
            csrf = pw_page.locator('meta[name="csrf-token"]').get_attribute("content", timeout=2000)
            if csrf:
                break
        except Exception:
            pass
        time.sleep(1)
    if not csrf:
        # No CSRF usually means DataDome served a challenge instead of the page.
        _sync_log("⚠️ Envato: нема CSRF (можливо DataDome) — переімпортуй cookies / перелогінься")
        return "needs_login"

    # ── Step 2: monthly aggregate totals + daily boundary ─────────────────
    detail = _fetch_json(
        pw_page,
        "/reports/api/v1/earnings/detail?view=monthly&start_date=2018-01-01&end_date=2030-12-31",
        csrf,
    )
    if "__error" in detail:
        _sync_log(f"⚠️ Envato: earnings/detail помилка: {detail['__error']}")
        return False
    rows = detail.get("data", [])
    if not rows:
        _sync_log("ℹ️ Envato: earnings/detail порожній")
        return True

    # boundary = first month with per-item (daily) data
    daily_first = detail.get("daily_first_record_date") or "2025-05-01"
    boundary_ym = daily_first[:7]
    month_totals = {}        # {YYYY-MM: float total (elements+market)}
    for r in rows:
        ym = r["date"][:7]
        month_totals[ym] = round(float(r.get("total_earnings", 0) or 0), 2)
    months_sorted = sorted(month_totals.keys())
    first_ym, last_ym = months_sorted[0], months_sorted[-1]
    _sync_log(f"📅 Envato: {first_ym}…{last_ym} ({len(months_sorted)} міс), "
              f"per-item з {boundary_ym}")

    prev_months = _load_months()
    new_months  = dict(prev_months)

    agg_saved = 0
    item_saved = 0
    months_touched = 0
    items_for_thumbs = {}   # item_id -> True (collected in per-item phase)

    # ── Step 3: walk every month ──────────────────────────────────────────
    for ym in _month_iter(first_ym, last_ym):
        if _sync_stop_flag[0]:
            break
        total = month_totals.get(ym, 0.0)
        if total <= 0:
            new_months[ym] = 0.0
            continue

        # Skip unchanged months (settling-delay aware: re-collect only on change)
        if abs(prev_months.get(ym, -1) - total) < 0.005:
            new_months[ym] = total
            continue

        months_touched += 1
        rec_date = _month_date(ym)

        if ym < boundary_ym:
            # ── historical AGGREGATE (no per-item data exists) ────────────
            # Replace any prior aggregate row for this month, then insert fresh.
            try:
                with sqlite3.connect(DB_NAME, timeout=15) as c:
                    c.execute("DELETE FROM sales WHERE stock='Envato' AND asset_id=?",
                              (f"envato-{ym}",))
            except Exception as ex:
                _app_log(f"[Envato] aggregate delete {ym} failed: {ex}")
            label = datetime.strptime(ym, "%Y-%m").strftime("%b %Y")
            _save_record({
                "asset_id":   f"envato-{ym}",
                "photo_name": f"Envato Elements — {label}",
                "stock":      "Envato",
                "price":      total,
                "date":       rec_date,
                "thumb_url":  "",
            })
            _make_month_card(ym)   # placard instead of a grey card
            agg_saved += 1
            new_months[ym] = total
        else:
            # ── PER-ITEM month ────────────────────────────────────────────
            # Delete this month's per-item rows (keep aggregates), then re-collect.
            try:
                with sqlite3.connect(DB_NAME, timeout=15) as c:
                    c.execute(
                        "DELETE FROM sales WHERE stock='Envato' "
                        "AND asset_id NOT LIKE 'envato-%' AND substr(date,1,7)=?",
                        (ym,))
            except Exception as ex:
                _app_log(f"[Envato] per-item delete {ym} failed: {ex}")

            y, m = int(ym[:4]), int(ym[5:7])
            last = calendar.monthrange(y, m)[1]
            start, end = f"{ym}-01", f"{ym}-{last:02d}"
            page_n, total_pages = 1, 1
            month_items = 0
            while not _sync_stop_flag[0]:
                data = _fetch_json(
                    pw_page,
                    f"/reports/api/v1/performance/item_performance"
                    f"?shopfront=Elements&start_date={start}&end_date={end}"
                    f"&sort_by=total_earnings&sort_direction=desc"
                    f"&new_item_only=false&page={page_n}",
                    csrf,
                )
                if "__error" in data:
                    _sync_log(f"⚠️ Envato {ym}: API error p{page_n}: {data['__error']}")
                    break
                items = data.get("data", [])
                if not items:
                    break
                total_pages = data.get("total_pages", 1)
                for it in items:
                    aid = str(it.get("item_id", ""))
                    earn = round(float(it.get("total_earnings", 0) or 0), 4)
                    if not aid or earn <= 0:
                        continue
                    _save_record({
                        "asset_id":   aid,
                        "photo_name": it.get("title", ""),
                        "stock":      "Envato",
                        "price":      earn,
                        "date":       rec_date,
                        "thumb_url":  "",
                        "filename":   it.get("filename", ""),
                    })
                    item_saved += 1
                    month_items += 1
                    if not _thumb_cached(aid):
                        items_for_thumbs[aid] = True
                if page_n >= total_pages:
                    break
                page_n += 1
                time.sleep(0.25)
            _sync_log(f"   {ym}: {month_items} items (${total})")
            new_months[ym] = total

    _save_months(new_months)
    _sync_log(f"✅ Envato: {months_touched} міс оновлено — "
              f"{agg_saved} агрегатів, {item_saved} per-item records")

    # ── Step 4: thumbnails for new per-item photos ────────────────────────
    if items_for_thumbs and not _sync_stop_flag[0]:
        _fetch_thumbnails(pw_page, list(items_for_thumbs.keys()))

    return True


def _fetch_thumbnails(pw_page, item_ids):
    """Fetch CLEAN (watermark-free) thumbnails from the contributor portfolio.

    portfolio.envato.com/items/search returns every approved item with
    {uuid, thumbnail_url}. The uuid == our per-item asset_id (verified), so we
    map by ID directly — no watermark, no visual matching. Replaces the old
    elements.envato.com og:image scrape (which was watermarked + cropped).

    load_img() runs ImageOps.fit→400x400 center-square (same pipeline as MS+),
    so the resulting asset_meta dHash matches MS+ thumbs → enables pHash grouping.
    """
    need = set(item_ids)
    _sync_log(f"🖼️ Envato: чисті thumbnails з портфоліо ({len(need)} потрібно)…")
    try:
        pw_page.goto(_PORTFOLIO_BASE + "/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
    except Exception as ex:
        _sync_log(f"⚠️ Envato: не вдалося відкрити portfolio.envato.com: {ex}")
        return

    # walk the approved-items list (newest first → new sales appear early)
    found = {}
    page, total_pages = 1, 1
    while need and page <= 200 and not _sync_stop_flag[0]:
        js = f"""async () => {{
            const r = await fetch("/items/search?status=distributable&page={page}"
                + "&per_page=100&sort=newest",
                {{credentials:"include", headers:{{"Accept":"application/json"}}}});
            if (!r.ok) return {{__error: r.status}};
            return await r.json();
        }}"""
        try:
            data = pw_page.evaluate(js)
        except Exception as ex:
            _sync_log(f"⚠️ Envato portfolio p{page}: {ex}")
            break
        if "__error" in data:
            _sync_log(f"⚠️ Envato portfolio p{page}: HTTP {data['__error']}")
            break
        items = data.get("items", [])
        if not items:
            break
        total_pages = (data.get("meta") or {}).get("total_pages", 1)
        for it in items:
            uid = it.get("uuid"); turl = it.get("thumbnail_url")
            if uid and turl:
                found[uid] = turl
                need.discard(uid)
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.15)

    # persist/merge the portfolio map (uuid -> thumbnail_url) for reuse
    try:
        prev = {}
        if os.path.exists(_PORTFOLIO_FILE):
            with open(_PORTFOLIO_FILE) as f:
                prev = json.load(f)
        prev.update(found)
        with open(_PORTFOLIO_FILE, "w") as f:
            json.dump(prev, f)
    except Exception:
        pass

    # download clean thumbs (load_img skips already-cached ids)
    dl = 0
    for aid in item_ids:
        if _sync_stop_flag[0]:
            break
        url = found.get(aid)
        if url:
            load_img_async(aid, url, None, is_adobe=False, stock="Envato")
            dl += 1
    _sync_log(f"   ✅ Envato thumbnails: {dl}/{len(item_ids)} clean (portfolio)")
