"""
collectors/freepik.py — Freepik (magnific.com) contributor earnings collector.

ISOLATION DRAFT (2026-06-08). NOT wired into orchestrator/UI yet.

DATA MODEL (confirmed via Inspector HAR, 2026-06-08):
  Host: contributor.magnific.com. Auth: cookie session + header
  `x-requested-with: XMLHttpRequest`. NO CSRF token (simpler than Envato).

  Endpoints:
    GET /xhr/user
        → { ..., "id"/"user_id": 30358497, ... }  — the contributor user_id.
    GET /xhr/stats/download?user_id={uid}&month=MM&year=YYYY
        → the per-asset MONTHLY CSV (application/octet-stream), identical columns
          to the manual export: Asset type, File name, Description, Asset public
          URL, Freepik Asset ID, Freepik Downloads, Freepik Earnings EUR.
          ← real per-asset per-month earnings. Backfill any month.
    GET /xhr/resource/published?limit=100&page=N
        → per-asset: { id, title, imgPreview (CLEAN img.magnific.com thumb),
          date (publication), downloads, earnings, aiGenerated }.
          ← clean thumbnails + (all-time?) per-asset earnings/downloads.
    GET /xhr/stats?format=monthly&dateStart=&dateEnd=
        → aggregate daily/monthly revenue + meta.totals.revenue. EUR.

CURRENCY: earnings are EUR. We convert per-month at the invoice date's EUR→USD
  ECB rate (frankfurter.app), cached in recipes/_fx_eur_usd.json. Stored as USD;
  the date < 2024-01 boundary keeps any pre-2024 estimate trivially separable.

GATE: invoices validate 4–10th of the following month. We only collect a month
  once it's final (current day >= 10 for the immediately-previous month), and
  never re-touch a month already in recipes/_freepik_months.json.

MATCHING: clean imgPreview → load_img() 400x400 center-square → asset_meta dHash
  → existing rebuild Pass F (_ms_visual_matches) groups Freepik cross-stock.

PRE-2024 (estimate): gated OFF (COLLECT_PRE2024) until we verify live whether
  portfolio `earnings` is all-time (→ exact) or not (→ download-weighted).
"""

import calendar
import csv as _csv
import io
import json
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests as _rq

from app_globals import DB_NAME, RECIPES_DIR, CACHE_DIR
from collectors.browser import _is_login_url
from image_utils import load_img_async
from sync_state import _app_log, _save_record, _sync_log, _sync_stop_flag

_BASE           = "https://contributor.magnific.com"
_MONTHS_FILE    = os.path.join(RECIPES_DIR, "_freepik_months.json")
_PORTFOLIO_FILE = os.path.join(RECIPES_DIR, "_freepik_portfolio.json")
_DL_RECENT_FILE = os.path.join(RECIPES_DIR, "_freepik_dl_recent.json")
_FX_FILE        = os.path.join(RECIPES_DIR, "_fx_eur_usd.json")

# Per-asset CSV (stats/download) only carries data from 2025-01 onward (verified
# live: all 2024 months return an empty CSV). Everything before is aggregate-only.
_CSV_FLOOR_YM   = "2025-01"
_HIST_BOUNDARY  = "2025-01"   # months < this have no per-asset data → estimate
# Pre-2025 estimate: real aggregate monthly revenue (anchor) distributed per-asset
# by all-time download weight. Portfolio `earnings` is always 0 so it can't give an
# exact split; portfolio `downloads` IS all-time (verified 185540 ≈ 179367).
COLLECT_PRE_HISTORY = True


# ── small state helpers ──────────────────────────────────────────────────────

def _load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, obj: dict):
    os.makedirs(RECIPES_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _month_iter(start_ym: str, end_ym: str):
    """Yield 'YYYY-MM' inclusive from start_ym..end_ym."""
    y, m = int(start_ym[:4]), int(start_ym[5:7])
    ey, em = int(end_ym[:4]), int(end_ym[5:7])
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            m = 1; y += 1


def _last_settled_ym(today: date | None = None) -> str:
    """Most recent month whose Freepik invoice has validated (4–10th next month).
    On/after the 10th the previous month is final; before the 10th step back one
    more so we never write a not-yet-invoiced month."""
    today = today or date.today()
    y, m = today.year, today.month
    m -= 1                                   # previous month
    if m == 0:
        m = 12; y -= 1
    if today.day < 10:                       # prev month not invoiced yet
        m -= 1
        if m == 0:
            m = 12; y -= 1
    return f"{y:04d}-{m:02d}"


def _month_date(ym: str) -> str:
    """Record date for a month: last day, capped at today (19-char via _save_record)."""
    y, m = int(ym[:4]), int(ym[5:7])
    d = date(y, m, calendar.monthrange(y, m)[1])
    if d > date.today():
        d = date.today()
    return d.strftime("%Y-%m-%d")


# ── EUR → USD (ECB via frankfurter), cached per month ────────────────────────

def _invoice_fx_date(ym: str) -> str:
    """The date whose EUR→USD rate we use for month `ym`: the 7th of the next
    month (mid invoice window), capped at today."""
    y, m = int(ym[:4]), int(ym[5:7])
    m += 1
    if m > 12:
        m = 1; y += 1
    d = date(y, m, 7)
    if d > date.today():
        d = date.today()
    return d.strftime("%Y-%m-%d")


_FX_FALLBACK_LEGACY = 1.08   # old hard-coded fallback; months stored with it are re-collected


def _eur_usd_rate(ym: str, _fetch=None):
    """EUR→USD for month `ym`, cached in _fx_eur_usd.json. `_fetch(date)->rate`
    is injectable for tests; defaults to frankfurter.app (ECB reference rates)."""
    cache = _load_json(_FX_FILE)
    if ym in cache and cache[ym].get("rate"):
        return float(cache[ym]["rate"])
    fxdate = _invoice_fx_date(ym)
    rate = None
    try:
        if _fetch is not None:
            rate = _fetch(fxdate)
        else:
            r = _rq.get(f"https://api.frankfurter.app/{fxdate}",
                        params={"from": "EUR", "to": "USD"}, timeout=15)
            if r.ok:
                rate = float(r.json()["rates"]["USD"])
    except Exception as ex:
        _app_log(f"[Freepik] FX fetch {ym} failed: {ex}")
    if not rate:
        # No rate → the caller must SKIP this month and retry next sync. The old
        # 1.08 fallback was "not cached" but the month was still marked collected
        # with that rate baked into every record (July 2026 was under-reported ~7%).
        return None
    cache[ym] = {"rate": round(rate, 6), "date": fxdate, "source": "frankfurter/ECB"}
    _save_json(_FX_FILE, cache)
    return rate


# ── CSV parsing (pure, unit-testable) ────────────────────────────────────────

_CSV_HEADER_MARK = "Freepik Asset ID"


def _looks_like_report_csv(text) -> bool:
    """True only for a real report body (has the CSV header). A login/HTML page
    served with HTTP 200, or an empty body, is NOT a report and must not be
    mistaken for "no sales this month"."""
    return bool(text) and _CSV_HEADER_MARK in text[:2000]


def _parse_report_csv(text: str) -> list[dict]:
    """Parse a /xhr/stats/download CSV into rows:
    {asset_id, filename, description, url, downloads, earnings_eur}."""
    out = []
    if not text:
        return out
    rdr = _csv.DictReader(io.StringIO(text))
    for r in rdr:
        aid = (r.get("Freepik Asset ID") or "").strip()
        if not aid:
            continue
        try:
            eur = float(r.get("Freepik Earnings EUR") or 0)
        except ValueError:
            eur = 0.0
        try:
            dl = int(r.get("Freepik Downloads") or 0)
        except ValueError:
            dl = 0
        out.append({
            "asset_id":     aid,
            "filename":     (r.get("File name") or "").strip(),
            "description":  (r.get("Description") or "").strip(),
            "url":          (r.get("Asset public URL") or "").strip(),
            "downloads":    dl,
            "earnings_eur": eur,
        })
    return out


# ── browser-context fetch helpers ────────────────────────────────────────────

def _fetch_text(get, path: str) -> str | None:
    """Transport-agnostic GET: `get` is a path->text|None callable (Playwright
    in-page fetch or direct requests). Kept as a function so all the call sites
    that pass it around read the same."""
    return get(path)


def _pw_get(pw_page):
    """Playwright transport: in-page fetch (cookie + x-requested-with)."""
    def get(path):
        js = f'''async () => {{
            const r = await fetch("{_BASE}{path}", {{
                credentials: "include",
                headers: {{"x-requested-with":"XMLHttpRequest","Accept":"*/*"}}
            }});
            if (!r.ok) return JSON.stringify({{__error: r.status}});
            return await r.text();
        }}'''
        try:
            t = pw_page.evaluate(js)
        except Exception as ex:
            _app_log(f"[Freepik] fetch {path} failed: {ex}")
            return None
        if isinstance(t, str) and t.startswith('{"__error"'):
            try:
                _app_log(f"[Freepik] {path} → HTTP {json.loads(t)['__error']}")
            except Exception:
                pass
            return None
        return t
    return get


def _http_get(sess):
    """Direct transport: requests.Session with native-login cookies."""
    def get(path):
        try:
            r = sess.get(_BASE + path, timeout=30)
        except Exception as ex:
            _app_log(f"[Freepik] direct {path} failed: {ex}")
            return None
        if not r.ok:
            _app_log(f"[Freepik] {path} → HTTP {r.status_code}")
            return None
        return r.text
    return get


def _fetch_json(get, path: str):
    t = _fetch_text(get, path)
    if t is None:
        return {"__error": "no body"}
    try:
        return json.loads(t)
    except Exception as ex:
        return {"__error": f"json: {ex}"}


def _get_user_id(get) -> str | None:
    d = _fetch_json(get, "/xhr/user")
    if "__error" in d:
        return None
    # the id may be nested under data/user
    for cand in (d, d.get("data") or {}, d.get("user") or {}):
        for k in ("user_id", "id", "userId"):
            v = cand.get(k) if isinstance(cand, dict) else None
            if v:
                return str(v)
    return None


# ── portfolio (clean thumbnails + per-asset all-time stats) ──────────────────

def _fetch_portfolio(get, limit: int = 100, max_pages: int = 400) -> dict:
    """Page /xhr/resource/published → {asset_id: {thumb, downloads, earnings,
    date, title}}. Merged into _freepik_portfolio.json for reuse.

    Early-stop: the endpoint is newest-first, so once a whole page contains only
    ids already in the cached portfolio file, everything below is known too —
    stop paging. Without this every sync walked all ~153 pages (15k assets) just
    to rediscover the same map. First run (empty cache) still walks everything."""
    prev = _load_json(_PORTFOLIO_FILE)
    out = {}
    page = 1
    while page <= max_pages and not _sync_stop_flag[0]:
        d = _fetch_json(get, f"/xhr/resource/published?limit={limit}&page={page}")
        if "__error" in d:
            _sync_log(f"⚠️ Freepik portfolio p{page}: {d['__error']}")
            break
        items = d.get("data") or []
        if not items:
            break
        page_new = 0
        for it in items:
            aid = str(it.get("id") or "")
            if not aid:
                continue
            if aid not in prev:
                page_new += 1
            out[aid] = {
                "thumb":     it.get("imgPreview") or "",
                "downloads": it.get("downloads") or 0,
                "earnings":  it.get("earnings") or 0,
                "date":      it.get("date") or "",
                "title":     it.get("title") or "",
            }
        if prev and page_new == 0:
            break                      # all-known page → rest is cached
        if len(items) < limit:
            break
        page += 1
    if out:
        prev.update(out)
        _save_json(_PORTFOLIO_FILE, prev)
    return prev if prev else out


# ── main collect ─────────────────────────────────────────────────────────────

def _freepik_collect_direct():
    """Direct-HTTPS collect with native-login cookies (no Playwright).
    Returns True / False (False → caller opens a native login)."""
    from collectors.session import stock_session
    sess = stock_session(["magnific.com", "freepik.com"], xhr=True, accept="*/*",
                         referer="https://contributor.magnific.com/statistics")
    if sess is None:
        _sync_log("⚠️ Freepik direct: нема cookies — потрібен нативний логін")
        return False
    res = _freepik_run(_http_get(sess))
    return False if res == "needs_login" else True


def _freepik_collect(pw_page):
    """Playwright fallback (in-page fetch). Returns True / 'needs_login'."""
    import time
    for _ in range(10):                 # let the SPA finish booting
        if not _is_login_url(pw_page.url):
            break
        time.sleep(0.5)
    return _freepik_run(_pw_get(pw_page))


def _freepik_run(get):
    """Core collection loop, transport-agnostic: get(path) -> text|None.
    Returns True / 'needs_login'."""
    _sync_log("📊 Freepik: перевіряємо сесію…")
    uid = _get_user_id(get)
    if not uid:
        _sync_log("⚠️ Freepik: не залогінений / нема user_id — Import Cookies або кнопка Freepik")
        return "needs_login"
    _sync_log(f"✅ Freepik: сесія активна (user_id={uid})")

    # 1) clean thumbnails map (also used by pre-2024 weighting later)
    portfolio = _fetch_portfolio(get)
    _sync_log(f"🖼️ Freepik: портфоліо {len(portfolio)} ассетів (чисті сабнейли)")

    # 2) per-asset monthly earnings — every settled month not yet collected
    months_state = _load_json(_MONTHS_FILE)
    end_ym = _last_settled_ym()
    total_saved = 0
    thumbs_needed = {}
    fetch_errors = 0
    for ym in _month_iter(_CSV_FLOOR_YM, end_ym):
        if _sync_stop_flag[0]:
            break
        prev = months_state.get(ym)
        recollect = False
        if isinstance(prev, dict) and (prev.get("rate_fallback")
                                       or prev.get("rate") == _FX_FALLBACK_LEGACY):
            # Month was stored with the hard-coded fallback rate → USD amounts are
            # wrong. Re-collect it with a real rate (rows of that month are replaced).
            recollect = True
        elif ym in months_state:             # final + already collected → never refetch
            continue
        yyyy, mm = ym[:4], ym[5:7]
        csv_text = _fetch_text(get, f"/xhr/stats/download?user_id={uid}&month={mm}&year={yyyy}")
        if not _looks_like_report_csv(csv_text):
            # Transport error, HTTP error, or an HTML page instead of the report:
            # DO NOT record the month as empty — that froze it at $0 forever.
            fetch_errors += 1
            _sync_log(f"  ⚠️ Freepik {ym}: report not available — will retry next sync")
            if fetch_errors >= 3:
                _sync_log("⚠️ Freepik: 3 report failures in a row — stopping this sync")
                break
            continue
        fetch_errors = 0
        rows = _parse_report_csv(csv_text)
        if not rows:
            months_state[ym] = {"total_eur": 0.0, "rows": 0}
            continue
        rate = _eur_usd_rate(ym)
        if rate is None:
            _sync_log(f"  ⚠️ Freepik {ym}: EUR→USD rate unavailable — month skipped, retry next sync")
            continue
        rec_date = _month_date(ym)
        if recollect:
            try:
                with sqlite3.connect(DB_NAME, timeout=15) as c:
                    n = c.execute("DELETE FROM sales WHERE stock='Freepik' AND substr(date,1,10)=?",
                                  (rec_date,)).rowcount
                _sync_log(f"  ♻️ Freepik {ym}: re-collecting with real rate {rate:.4f} (replaced {n} rows @ {_FX_FALLBACK_LEGACY})")
            except Exception as ex:
                _sync_log(f"  ⚠️ Freepik {ym}: could not replace fallback-rate rows: {ex}")
                continue
        month_eur = 0.0
        for r in rows:
            eur = r["earnings_eur"]
            if eur <= 0:
                continue
            month_eur += eur
            _save_record({
                "asset_id":   r["asset_id"],
                "photo_name": r["description"],
                "stock":      "Freepik",
                "price":      round(eur * rate, 4),
                "date":       rec_date,
                "thumb_url":  portfolio.get(r["asset_id"], {}).get("thumb", ""),
                "filename":   r["filename"],
            })
            total_saved += 1
            if r["asset_id"] in portfolio:
                thumbs_needed[r["asset_id"]] = portfolio[r["asset_id"]]["thumb"]
        months_state[ym] = {"total_eur": round(month_eur, 2),
                            "rate": round(rate, 6), "rows": len(rows)}
        _sync_log(f"   {ym}: {len(rows)} ассетів, €{month_eur:.2f} @ {rate:.4f}")
    _save_json(_MONTHS_FILE, months_state)
    _sync_log(f"✅ Freepik: {total_saved} per-asset records (2024+)")

    # 3) download clean thumbs for newly-collected assets
    dl = 0
    for aid, turl in thumbs_needed.items():
        if _sync_stop_flag[0]:
            break
        if turl:
            load_img_async(aid, turl, None, is_adobe=False, stock="Freepik")
            dl += 1
    _sync_log(f"🖼️ Freepik thumbnails: {dl} clean queued")

    # 4) backfill: download thumbs for any DB asset still missing from img_cache
    cached_set = set(f[:-4] for f in os.listdir(CACHE_DIR) if f.endswith('.jpg')) if os.path.isdir(CACHE_DIR) else set()
    missing_thumbs = []
    with sqlite3.connect(DB_NAME, timeout=15) as _c:
        for aid, turl in _c.execute(
            "SELECT DISTINCT asset_id, thumb_url FROM sales "
            "WHERE stock='Freepik' AND thumb_url IS NOT NULL AND thumb_url != ''"
        ).fetchall():
            if aid and aid not in cached_set:
                missing_thumbs.append((aid, turl))
    if missing_thumbs:
        _sync_log(f"🖼️ Freepik backfill: {len(missing_thumbs)} missing thumbs queued")
        for aid, turl in missing_thumbs:
            if _sync_stop_flag[0]:
                break
            load_img_async(aid, turl, None, is_adobe=False, stock="Freepik")

    if COLLECT_PRE_HISTORY and not _sync_stop_flag[0]:
        _collect_pre_history(get, uid, portfolio, end_ym)

    return True


def _recent_downloads(get, uid: str, end_ym: str) -> dict:
    """Per-asset downloads in the per-asset (2025+) era, cached in
    _freepik_dl_recent.json so we fetch each month's CSV only once ever. Needed to
    derive pre-2025 downloads = portfolio all-time − this."""
    cache = _load_json(_DL_RECENT_FILE)
    months = set(cache.get("months") or [])
    dl = {k: int(v) for k, v in (cache.get("dl") or {}).items()}
    changed = False
    for ym in _month_iter(_HIST_BOUNDARY, end_ym):
        if ym in months or _sync_stop_flag[0]:
            continue
        yyyy, mm = ym[:4], ym[5:7]
        txt = _fetch_text(get, f"/xhr/stats/download?user_id={uid}&month={mm}&year={yyyy}")
        for r in _parse_report_csv(txt or ""):
            dl[r["asset_id"]] = dl.get(r["asset_id"], 0) + r["downloads"]
        months.add(ym); changed = True
    if changed:
        _save_json(_DL_RECENT_FILE, {"months": sorted(months), "dl": dl})
    return dl


def _collect_pre_history(get, uid: str, portfolio: dict, end_ym: str):
    """ESTIMATE pre-2025 earnings (Freepik has no per-asset data before 2025-01).
    Anchor = REAL aggregate monthly revenue (/xhr/stats); split per-asset by
    all-time download weight; one record per (asset, year) dated year-end. Stored
    as USD; the date < 2025-01 boundary keeps the estimate trivially separable.

    Only the per-photo SPLIT is modeled — each year's TOTAL equals the real Freepik
    payout for that year. portfolio `earnings` is always 0 so it can't give an exact
    split; portfolio `downloads` is all-time (verified)."""
    # COLLECT ONCE, NEVER AGAIN: like every other stock doesn't re-walk history to
    # day one each sync, the pre-2025 estimate is written a single time. If the DB
    # already has any Freepik pre-2025 rows, skip entirely (no fetch, no re-insert).
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as _c:
            if _c.execute("SELECT 1 FROM sales WHERE stock='Freepik' "
                          "AND date < '2025-01-01' LIMIT 1").fetchone():
                _sync_log("⏩ Freepik pre-2025: вже зібрано раніше — пропускаю")
                return
    except Exception:
        pass
    _sync_log("📜 Freepik: оцінка історії до 2025 (агрегат × вага завантажень)…")

    # 1) real aggregate monthly revenue (EUR) for months < 2025-01
    agg = _fetch_json(get,
        "/xhr/stats?format=monthly&license=all&typeFile=all"
        "&dateStart=2006-01-01&dateEnd=2024-12-31")
    data = (agg or {}).get("data") or {}
    cats = data.get("categories") or []
    rev = next((s.get("data", []) for s in data.get("series", [])
                if s.get("name") == "Revenue"), [])
    month_eur = {c: float(r) for c, r in zip(cats, rev)
                 if r and float(r) > 0 and c < _HIST_BOUNDARY}
    if not month_eur:
        _sync_log("ℹ️ Freepik: агрегат до 2025 порожній — нічого оцінювати")
        return

    # IDEMPOTENT: pre-2025 history is immutable. Skip the whole estimate (no
    # re-fetch, no DELETE+re-INSERT) unless the historical aggregate or the
    # portfolio size changed — so it isn't re-collected on every sync (which made
    # the records jump to the top of the feed and flash blue each time).
    import hashlib
    fp_src = json.dumps({k: round(v, 2) for k, v in sorted(month_eur.items())},
                        sort_keys=True) + f"|assets={len(portfolio)}"
    fp = hashlib.sha1(fp_src.encode()).hexdigest()
    st = _load_json(_MONTHS_FILE)
    if st.get("_pre2025_fp") == fp:
        _sync_log("⏩ Freepik pre-2025: незмінне — пропускаю (вже зібрано)")
        return

    # 2) pre-2025 download weight = all-time(portfolio) − 2025+ downloads
    recent = _recent_downloads(get, uid, end_ym)
    pre_dl = {}
    for aid, info in portfolio.items():
        pre = int(info.get("downloads") or 0) - int(recent.get(aid, 0))
        if pre > 0:
            pre_dl[aid] = (pre, (info.get("date") or "")[:4])   # (weight, pub-year)
    if not pre_dl:
        _sync_log("ℹ️ Freepik: нема pre-2025 завантажень для розподілу")
        return

    # 3) real USD per pre-2025 year
    year_usd = defaultdict(float)
    for ym, eur in month_eur.items():
        year_usd[ym[:4]] += eur * _eur_usd_rate(ym)

    # 4) wipe any prior estimate, then distribute per year by download weight
    try:
        with sqlite3.connect(DB_NAME, timeout=30) as c:
            c.execute("DELETE FROM sales WHERE stock='Freepik' AND date < '2025-01-01'")
    except Exception as ex:
        _app_log(f"[Freepik] pre-2025 wipe failed: {ex}")
    saved = 0
    for year in sorted(year_usd):
        usd = year_usd[year]
        if usd <= 0 or _sync_stop_flag[0]:
            continue
        elig = {aid: w for aid, (w, pub) in pre_dl.items() if not pub or pub <= year}
        tot = sum(elig.values())
        if tot <= 0:
            continue
        rec_date = f"{year}-12-31"
        for aid, w in elig.items():
            amt = usd * (w / tot)
            if amt < 0.0001:
                continue
            info = portfolio.get(aid, {})
            _save_record({
                "asset_id":   aid,
                "photo_name": info.get("title", ""),
                "stock":      "Freepik",
                "price":      round(amt, 4),
                "date":       rec_date,
                "thumb_url":  info.get("thumb", ""),
            })
            saved += 1
    st = _load_json(_MONTHS_FILE)
    st["_pre2025_fp"] = fp
    _save_json(_MONTHS_FILE, st)
    _sync_log(f"✅ Freepik pre-2025 estimate: {saved} записів, "
              f"{len(year_usd)} років, ${sum(year_usd.values()):.0f} (€-anchored)")
