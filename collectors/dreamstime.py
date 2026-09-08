"""
collectors/dreamstime.py — Dreamstime (dreamstime.com) contributor earnings.

DATA MODEL (confirmed via Inspector, 2026-06-13):
  Host: www.dreamstime.com. Auth: plain cookie session (the earnings page is a
  normal authenticated HTML GET). One flat list grouped by date sections.

  Page: GET /account/earnings-images?showlicense=all&pg=N
    → HTML. A date header precedes each day's items:
        <div class="account-list__date">June 1, 2026</div>
        <div class="account-list__item ...">
          <div class="account-list__thumb">
            <a href="/account/earnings-stats?imageid=283450379">
              <img src="https://thumbs.dreamstime.com/m/...-283450379.jpg">
          <a class="account-list__title">Title</a>
          <p><strong>$0.35</strong> ... subscription ... maximum </p>
    Newest first. ~38 items/page. Walk pg=1,2,3… until empty / all-known.

  ⚠️ Dreamstime gives a DATE only (no per-sale timestamp), so dedup falls back to
  day+price per image — two identical-price sales of the same image on the same
  day collapse to one (same limitation as Depositphotos / SS daily aggregates).
  No stable per-sale id exists to do better.

STATE: none — relies on _save_record's day+price dedup for date-only sales.
"""

import re
from datetime import datetime

from db import is_already_saved
from sync_state import _app_log, _save_record, _sync_log, _sync_stop_flag
from collectors.browser import _is_login_url

_BASE  = "https://www.dreamstime.com"
_STOCK = "Dreamstime"

_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
               "image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.dreamstime.com/account/earnings-images",
}


def _dreamstime_collect_direct():
    """Collect via direct HTTPS with cookies imported from the user's NATIVE Chrome
    login (no Playwright — PerimeterX blocks CDP automation). Returns True if it
    pulled data, False if blocked / no session (→ caller opens a native login)."""
    import requests as _rq
    from cookies import _load_browser_cookies

    jar = {}
    for c in _load_browser_cookies():
        dom = (c.get("domain") or "").lstrip(".")
        if "dreamstime.com" in dom:
            jar[c["name"]] = c["value"]
    if not jar:
        _sync_log("⚠️ Dreamstime direct: нема cookies — потрібен нативний логін")
        return False

    sess = _rq.Session()
    sess.headers.update(_BROWSER_HEADERS)
    for k, v in jar.items():
        sess.cookies.set(k, v, domain=".dreamstime.com")

    saved, page, prev_fp = 0, 1, None
    while page <= 500 and not _sync_stop_flag[0]:
        path = "/account/earnings-images?showlicense=all"
        if page > 1:
            path += f"&pg={page}"
        try:
            r = sess.get(_BASE + path, timeout=30)
        except Exception as ex:
            _sync_log(f"⚠️ Dreamstime direct: {ex}")
            return saved > 0
        if r.status_code in (401, 403) or _is_login_url(r.url):
            if page == 1:
                _sync_log(f"⚠️ Dreamstime direct: HTTP {r.status_code} — сесія недійсна/блок")
                return False
            break
        rows = _parse_page(r.text)
        if not rows:
            break
        fp = (rows[0]["asset_id"], rows[0]["date"], rows[-1]["asset_id"])
        if fp == prev_fp:
            break
        prev_fp = fp
        new_here = _save_rows(rows)
        saved += new_here
        _sync_log(f"Dreamstime direct: стор. {page} — {len(rows)} рядків, +{new_here}")
        if new_here == 0:
            break
        page += 1
    _sync_log(f"✅ Dreamstime direct: збережено {saved} записів")
    return True


def _save_rows(rows) -> int:
    """Save parsed rows via _save_record (dedup), return count of NEW rows. Shared
    by the direct and Playwright paths."""
    from image_utils import load_img_async
    new_here = 0
    for r in rows:
        if r["price"] <= 0:
            continue
        is_new = not is_already_saved(_STOCK, r["asset_id"], r["price"], r["date"])
        _save_record({"stock": _STOCK, "asset_id": r["asset_id"], "price": r["price"],
                      "date": r["date"], "thumb_url": r["thumb"]})
        if is_new:
            new_here += 1
            if r["thumb"]:
                load_img_async(r["asset_id"], r["thumb"], None, is_adobe=False, stock=_STOCK)
    return new_here


def _parse_page(html: str) -> list[dict]:
    """Parse one earnings page → [{asset_id, thumb, date, price, license}].
    Walks the markup in order so each item inherits the last date header."""
    out = []
    cur_date = None
    # Split on date headers and item starts, keeping order via a single scan.
    token_re = re.compile(
        r'<div class="account-list__date">([^<]+)</div>'
        r'|<div class="account-list__item')
    parts = token_re.split(html)
    # parts: [pre, date1|None, chunk1, date2|None, chunk2, ...]
    i = 1
    while i < len(parts):
        date_tok = parts[i]
        chunk = parts[i + 1] if i + 1 < len(parts) else ""
        if date_tok:                      # a date header
            cur_date = _norm_date(date_tok)
        else:                             # an item chunk
            rec = _parse_item(chunk, cur_date)
            if rec:
                out.append(rec)
        i += 2
    return out


def _parse_item(chunk: str, cur_date) -> dict | None:
    mid = re.search(r'imageid=(\d+)', chunk) or re.search(r'-image(\d+)"', chunk)
    if not mid or not cur_date:
        return None
    aid = mid.group(1)
    thumb_m = re.search(r'<img[^>]+src="(https://thumbs\.dreamstime\.com/[^"]+)"', chunk)
    thumb = thumb_m.group(1) if thumb_m else ""
    price_m = re.search(r'<strong>\$([\d.]+)</strong>', chunk)
    if not price_m:
        return None
    price = float(price_m.group(1))
    lic_m = re.search(r'</strong>(.*?)</p>', chunk, re.S)
    lic = re.sub(r'<[^>]+>', ' ', lic_m.group(1)).split() if lic_m else []
    return {"asset_id": aid, "thumb": thumb, "date": cur_date,
            "price": price, "license": " ".join(lic[:2])}


def _norm_date(s: str):
    """'June 1, 2026' → '2026-06-01'."""
    try:
        return datetime.strptime(s.strip(), "%B %d, %Y").strftime("%Y-%m-%d")
    except Exception:
        return None


def _fetch_html(pw_page, path: str) -> str | None:
    """Canonical pattern (CLAUDE.md): fetch from INSIDE the page so the request
    is same-origin and carries the session + any WAF clearance the shared
    chrome_profile already passed during manual login. A separate goto() with a
    query string reads as a direct bot hit and gets 403'd."""
    js = f"""async () => {{
        const r = await fetch("{_BASE}{path}", {{
            credentials: "include",
            headers: {{"Accept": "text/html, */*"}}
        }});
        if (!r.ok) return "__ERR__" + r.status;
        return await r.text();
    }}"""
    try:
        t = pw_page.evaluate(js)
    except Exception as ex:
        _app_log(f"[Dreamstime] fetch {path} failed: {ex}")
        return None
    if t.startswith("__ERR__"):
        _app_log(f"[Dreamstime] {path} → HTTP {t[7:]}")
        return None
    return t


def _dreamstime_collect(pw_page):
    """Collect Dreamstime earnings. Returns True / 'needs_login'."""
    from image_utils import load_img_async
    import time

    _sync_log("📊 Dreamstime: перевіряємо сесію…")
    pw_page.goto(f"{_BASE}/account/earnings-images",
                 wait_until="domcontentloaded", timeout=45000)
    time.sleep(2)
    if _is_login_url(pw_page.url):
        _sync_log("⚠️ Dreamstime: не залогінений — натисни кнопку «Dreamstime» щоб увійти")
        return "needs_login"
    _sync_log("✅ Dreamstime: сесія активна")

    saved = 0
    page = 1
    prev_fingerprint = None
    while page <= 500 and not _sync_stop_flag[0]:
        # Page 1 carries no `pg` param (matches the real site); pages 2+ add &pg=N.
        path = "/account/earnings-images?showlicense=all"
        if page > 1:
            path += f"&pg={page}"
        html = _fetch_html(pw_page, path)
        if html is None:
            # A 403 on the very first page = WAF blocked us / session not cleared.
            # Surface it as needs_login so the uniform handler opens a window for
            # the user to pass Dreamstime's anti-bot challenge interactively.
            if page == 1 and saved == 0:
                _sync_log("⚠️ Dreamstime: доступ заблоковано (403) — відкриваю вікно")
                return "needs_login"
            break
        rows = _parse_page(html)
        if not rows:
            break
        # Guard against a site that repeats the last page past the end.
        fp = (rows[0]["asset_id"], rows[0]["date"], rows[-1]["asset_id"])
        if fp == prev_fingerprint:
            break
        prev_fingerprint = fp
        new_here = 0
        for r in rows:
            if r["price"] <= 0:
                continue
            is_new = not is_already_saved(_STOCK, r["asset_id"], r["price"], r["date"])
            _save_record({
                "stock":     _STOCK,
                "asset_id":  r["asset_id"],
                "price":     r["price"],
                "date":      r["date"],
                "thumb_url": r["thumb"],
            })
            if is_new:
                new_here += 1
                saved += 1
                if r["thumb"]:
                    load_img_async(r["asset_id"], r["thumb"], None, is_adobe=False, stock=_STOCK)
        _sync_log(f"Dreamstime: стор. {page} — {len(rows)} рядків, +{new_here} нових")
        if new_here == 0:
            break
        page += 1
    _sync_log(f"✅ Dreamstime: збережено {saved} записів")
    return True
