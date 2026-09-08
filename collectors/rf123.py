"""
collectors/rf123.py — 123RF contributor earnings collector.

DATA MODEL (confirmed via Inspector, 2026-06-12):
  Host: www.123rf.com, API base /apicore-contributors/. Auth: plain cookie
  session (no CSRF, no bearer). All GET.

  Endpoints:
    GET /apicore-contributors/profile
        → 401 even with a valid session (SPA adds extra context) — NOT usable
          as the session check; the monthly report is the check instead.
    GET /apicore-contributors/earnings/report/monthly
        → {"data":[{"year":..,"total_earnings":"$ 97.25",
           "data_per_month":[{"simple_date":"2026-06","total_earnings":"$ 6.51"},..]},..]}
          All years back to account creation (real data from 2020).
    GET /apicore-contributors/earnings/report/daily?file_type=all&date=YYYY-MM
        → {"data":{"data_per_day":[..]}} / list of per-day rows with
          "simple_date":"YYYY-MM-DD" and "total_earnings":"$0.25".
          Lets us skip zero days entirely.
    GET /apicore-contributors/earnings/report/download_stats?page=N&limit=100&date=YYYY-MM-DD
        → {"data":[{stockId, totalNet, totalEarning, fileName,
           iso8601DateTime, thumbnails.nonWatermarkedUs450, refund_date}],
           "meta":{"pagination":{total_pages,..}}}
          ← per-sale records with exact timestamp and a WATERMARK-FREE thumb.

STATE: recipes/_123rf_months.json {YYYY-MM: monthly_total_float}. A month is
  re-collected only when its monthly total changed (handles late postings and
  refunds); per-sale dedup is exact 19-char timestamped via _save_record.
"""

import json
import os
from datetime import date

from app_globals import RECIPES_DIR
from image_utils import load_img_async
from sync_state import _app_log, _save_record, _sync_log, _sync_stop_flag

_BASE        = "https://www.123rf.com/apicore-contributors"
_MONTHS_FILE = os.path.join(RECIPES_DIR, "_123rf_months.json")
_STOCK       = "123RF"


def _load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, d: dict):
    try:
        with open(path, "w") as f:
            json.dump(d, f, indent=1)
    except Exception as ex:
        _app_log(f"[123RF] save {os.path.basename(path)} failed: {ex}")


def _money(s) -> float:
    """'$ 6.51' / '$0.25' / 0.216 → float."""
    try:
        return float(str(s).replace("$", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


# ── browser-context fetch ────────────────────────────────────────────────────

def _fetch_json(pw_page, path: str):
    js = f"""async () => {{
        const r = await fetch("{_BASE}{path}", {{
            credentials: "include",
            headers: {{"Accept": "application/json, text/plain, */*"}}
        }});
        if (!r.ok) return JSON.stringify({{__error: r.status}});
        return await r.text();
    }}"""
    try:
        t = pw_page.evaluate(js)
    except Exception as ex:
        _app_log(f"[123RF] fetch {path} failed: {ex}")
        return {"__error": str(ex)}
    try:
        d = json.loads(t)
    except Exception as ex:
        return {"__error": f"json: {ex}"}
    if isinstance(d, dict) and "__error" in d:
        _app_log(f"[123RF] {path} → HTTP {d['__error']}")
    return d


# ── report parsing (pure) ────────────────────────────────────────────────────

def _month_totals(monthly_resp: dict) -> dict:
    """monthly report → {YYYY-MM: total_float} for months with earnings > 0."""
    out = {}
    for yr in (monthly_resp.get("data") or []):
        for m in (yr.get("data_per_month") or []):
            ym = m.get("simple_date") or ""
            tot = _money(m.get("total_earnings"))
            if ym and tot > 0:
                out[ym] = tot
    return out


def _nonzero_days(daily_resp: dict) -> list[str]:
    """daily report → ['YYYY-MM-DD', ...] days with earnings > 0."""
    d = daily_resp.get("data")
    rows = d.get("data_per_day") if isinstance(d, dict) else d
    if not isinstance(rows, list):
        # some shapes nest one level deeper; find the first list of day rows
        rows = []
        if isinstance(d, dict):
            for v in d.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "simple_date" in v[0]:
                    rows = v
                    break
    out = []
    for r in rows or []:
        if _money(r.get("total_earnings")) > 0 and r.get("simple_date"):
            out.append(r["simple_date"])
    return sorted(out)


def _norm_dt(iso: str, day: str) -> str:
    """'2026-06-05T03:02:47-04:00' → '2026-06-05T03:02:47' (stock-local naive).
    Any timestamped form is fine for _save_record (it parses ISO-T and the app's
    own 'YYYY-MM-DD HH:MM:SS', and drops — never stamps 'now' on — unparsable dates)."""
    if iso and len(iso) >= 19:
        return iso[:19]
    return day + "T00:00:00"


# ── main collect ─────────────────────────────────────────────────────────────

def _rf123_collect_direct():
    """Direct-HTTPS collect with cookies from the native Chrome login (no Playwright).
    Returns True / False (False → caller opens a native login)."""
    from collectors.session import stock_session
    sess = stock_session(
        ["123rf.com"], accept="application/json, text/plain, */*",
        referer="https://www.123rf.com/contributor/earning-details")
    if sess is None:
        _sync_log("⚠️ 123RF direct: нема cookies — потрібен нативний логін")
        return False

    def get_json(path):
        try:
            r = sess.get(_BASE + path, timeout=30)
            if not r.ok:
                return {"__error": r.status_code}
            return r.json()
        except Exception as ex:
            return {"__error": str(ex)}

    res = _rf123_run(get_json)
    if res == "needs_login":
        return False
    return res


def _rf123_collect(pw_page):
    """Playwright fallback (in-page fetch). Returns True / 'needs_login'."""
    return _rf123_run(lambda p: _fetch_json(pw_page, p))


def _rf123_run(get_json):
    """Core collection loop, transport-agnostic: get_json(path) -> dict.
    Returns True / 'needs_login'."""
    import time as _t
    # NOTE: /profile 401s even with a valid session (needs extra context the SPA
    # provides) — the monthly report itself is the session check. A fetch fired
    # before the SPA finishes booting can 401 transiently → retry a few times.
    monthly = None
    for _try in range(4):
        monthly = get_json("/earnings/report/monthly")
        if isinstance(monthly, dict) and "__error" not in monthly:
            break
        _t.sleep(4)
    if not isinstance(monthly, dict) or "__error" in monthly:
        err = monthly.get("__error") if isinstance(monthly, dict) else monthly
        # Only an auth-class status means the session is gone. A 5xx/timeout is
        # transient: report it and skip this sync instead of popping a login window.
        if err in (401, 403, "401", "403"):
            _sync_log("⚠️ 123RF: не залогінений — натисни кнопку «123RF» щоб увійти")
            return "needs_login"
        _sync_log(f"⚠️ 123RF: monthly report недоступний ({err}) — пропускаю цей синк")
        return "transient"
    _sync_log("✅ 123RF: сесія активна")
    totals = _month_totals(monthly)
    if not totals:
        _sync_log("⚠️ 123RF: monthly report порожній")
        return True

    state = _load_json(_MONTHS_FILE)
    cur_ym = date.today().strftime("%Y-%m")
    todo = [ym for ym, tot in totals.items()
            if ym == cur_ym or abs(state.get(ym, -1) - tot) > 0.005]
    todo.sort(reverse=True)  # recent first
    if not todo:
        _sync_log("123RF: нових місяців немає")
        return True
    _sync_log(f"123RF: збираємо {len(todo)} місяців…")

    saved = 0
    pending = 0
    for ym in todo:
        if _sync_stop_flag[0]:
            break
        daily = get_json(f"/earnings/report/daily?file_type=all&date={ym}")
        if not isinstance(daily, dict) or "__error" in daily:
            # The daily report failed → we cannot know which days to fetch.
            # Do NOT record the month total: the month would be considered done
            # with zero per-sale rows until its total changes again.
            _sync_log(f"  ⚠️ 123RF {ym}: daily report failed — month left pending")
            pending += 1
            continue
        days = _nonzero_days(daily)
        month_ok = True
        if not days and totals.get(ym, 0) > 0:
            # Report parsed but no day rows although the month has earnings:
            # shape drift or partial response — don't mark it done.
            _sync_log(f"  ⚠️ 123RF {ym}: total ${totals[ym]:.2f} but no day rows — month left pending")
            month_ok = False
        for day in days:
            if _sync_stop_flag[0]:
                month_ok = False
                break
            page, pages = 1, 1
            while page <= pages:
                d = get_json(
                    f"/earnings/report/download_stats?page={page}&limit=100&date={day}")
                if not isinstance(d, dict) or "__error" in d:
                    month_ok = False
                    break
                pg = ((d.get("meta") or {}).get("pagination") or {})
                pages = int(pg.get("total_pages") or 1)
                for rec in (d.get("data") or []):
                    if rec.get("refund_date"):
                        continue
                    aid = str(rec.get("stockId") or "")
                    net = _money(rec.get("totalNet") or rec.get("totalEarning"))
                    if not aid or net <= 0:
                        continue
                    turl = (rec.get("thumbnails") or {}).get("nonWatermarkedUs450") or ""
                    _save_record({
                        "stock":      _STOCK,
                        "asset_id":   aid,
                        "price":      round(net, 3),
                        "date":       _norm_dt(rec.get("iso8601DateTime") or "", day),
                        "photo_name": rec.get("fileName") or "",
                        "thumb_url":  turl,
                    })
                    saved += 1
                    if turl:
                        load_img_async(aid, turl, None, is_adobe=False, stock=_STOCK)
                page += 1
        if month_ok:
            state[ym] = totals[ym]
            _save_json(_MONTHS_FILE, state)
            _sync_log(f"123RF: {ym} готово (${totals[ym]:.2f})")
        else:
            pending += 1
    _sync_log(f"✅ 123RF: збережено {saved} записів")
    if pending and not _sync_stop_flag[0]:
        return "transient"
    return True
