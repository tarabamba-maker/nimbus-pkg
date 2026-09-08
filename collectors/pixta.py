"""
collectors/pixta.py — PIXTA (pixtastock.com) contributor earnings collector.

DATA MODEL (confirmed via Inspector, 2026-06-13):
  Host: www.pixtastock.com. Auth: plain cookie session (the earnings page is a
  normal authenticated HTML GET — no CSRF needed for it; CSRF only guards the
  footer_campaigns XHR). One flat list like Adobe / 123RF.

  Page: GET /mypage/earning?earned_from=all&content_type=&year=&month=
                            &per_page=200&page=N
    → HTML table, one row per sale:
        <tr class="sale-chistory">
          <td><a href="/photo/{id}"><img src="https://en.pimg.jp/.../{id}.jpg"></a>
          <td><a href="/mypage/manager/detail/{id}">{id}</a>
          <td>{type icon}                       (PHOTO / ILLUST / VIDEO)
          <td><p>MM/DD/YYYY HH:MM</p>           (earned-credit datetime)
          <td>{earnings}                        (net, USD — e.g. 0.27)
    Newest first. 200 rows/page. Lifetime total shown as "Your Lifetime
    Earnings". Walk pages until one yields no NEW rows (incremental) or runs
    short of 200 (last page).

STATE: none of its own — relies on _save_record's exact-timestamp dedup. The
  per-sale datetime makes each sale distinct, so a re-walk is idempotent.
"""

import re

from db import is_already_saved
from sync_state import _app_log, _save_record, _sync_log, _sync_stop_flag
from collectors.browser import _is_login_url

_BASE     = "https://www.pixtastock.com"
_STOCK    = "PIXTA"
_PER_PAGE = 200


def _parse_rows(html: str) -> list[dict]:
    """Parse one earnings page → [{asset_id, thumb, date, price, type}]."""
    out = []
    for block in re.split(r'<tr class="sale-chistory">', html)[1:]:
        block = block.split("</tr>")[0]
        mid = re.search(r'/mypage/manager/detail/(\d+)', block) \
              or re.search(r'/photo/(\d+)', block)
        if not mid:
            continue
        aid = mid.group(1)
        thumb_m = re.search(r'<img[^>]+src="([^"]+)"', block)
        thumb = thumb_m.group(1) if thumb_m else ""
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        # Thumbnail size code in the path: /0/=120px CLEAN but squished aspect
        # (1.30 vs true 1.46) → bad pHash; /1/=450px and /2/=1000px carry a centered
        # PIXTA watermark + correct aspect. /2/ (1000px) is the sweet spot: at high
        # res the watermark is proportionally tiny, so dHash's 9x8 downscale barely
        # sees it (twin hamming ~2), while the aspect matches the other stocks.
        # /1/ (450px) is too small — the watermark dominates there (hamming ~7).
        thumb = re.sub(r'(://[^/]+/\d+/\d+/\d+/)\d(/)', r'\g<1>2\g<2>', thumb)
        date_m = re.search(r'(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})', block)
        if not date_m:
            continue
        mm, dd, yyyy, hh, mi = date_m.groups()
        date = f"{yyyy}-{mm}-{dd}T{hh}:{mi}:00"
        # earnings = last bare decimal in the row (the Earnings <td>)
        nums = re.findall(r'>\s*(\d+\.\d{2})\s*<', block)
        price = float(nums[-1]) if nums else 0.0
        typ_m = re.search(r'class="com-ico-(\w+)"', block)
        out.append({
            "asset_id": aid,
            "thumb":    thumb,
            "date":     date,
            "price":    price,
            "type":     (typ_m.group(1) if typ_m else "photo"),
        })
    return out


def _fetch_html(pw_page, path: str) -> str | None:
    js = f"""async () => {{
        const r = await fetch("{_BASE}{path}", {{
            credentials: "include",
            headers: {{"x-requested-with": "XMLHttpRequest",
                       "Accept": "text/html, */*; q=0.01"}}
        }});
        if (!r.ok) return JSON.stringify({{__error: r.status}});
        return await r.text();
    }}"""
    try:
        t = pw_page.evaluate(js)
    except Exception as ex:
        _app_log(f"[PIXTA] fetch {path} failed: {ex}")
        return None
    if t.startswith('{"__error"'):
        _app_log(f"[PIXTA] {path} → HTTP {t}")
        return None
    return t


def _pixta_collect_direct():
    """Direct-HTTPS collect with native-login cookies (no Playwright).
    Returns True / False (False → caller opens a native login)."""
    from collectors.session import stock_session
    sess = stock_session(["pixtastock.com", "pixta"], xhr=True,
                         accept="text/html, */*; q=0.01",
                         referer="https://www.pixtastock.com/mypage/earning")
    if sess is None:
        _sync_log("⚠️ PIXTA direct: нема cookies — потрібен нативний логін")
        return False

    def get_html(path):
        """text | None (session invalid: login redirect / 401 / 403) |
        False (transient: other HTTP error or network exception)."""
        try:
            r = sess.get(_BASE + path, timeout=30)
            if "user_login" in r.url or r.status_code in (401, 403):
                return None
            if not r.ok:
                _app_log(f"[PIXTA] direct {path}: HTTP {r.status_code}")
                return False
            return r.text
        except Exception as ex:
            _app_log(f"[PIXTA] direct {path}: {ex}")
            return False

    res = _pixta_run(get_html)
    if res == "needs_login":
        return False
    return res


def _pixta_collect(pw_page):
    """Playwright fallback (in-page fetch). Returns True / 'needs_login'."""
    import time
    _sync_log("📊 PIXTA: перевіряємо сесію…")
    pw_page.goto(f"{_BASE}/mypage/earning", wait_until="domcontentloaded", timeout=45000)
    time.sleep(2)
    if _is_login_url(pw_page.url) or "user_login" in pw_page.url:
        _sync_log("⚠️ PIXTA: не залогінений — натисни кнопку «PIXTA» щоб увійти")
        return "needs_login"
    return _pixta_run(lambda p: _fetch_html(pw_page, p))


def _pixta_run(get_html):
    """Core collection loop, transport-agnostic: get_html(path) -> str|None.
    Returns True / 'needs_login'."""
    from image_utils import load_img_async

    saved = 0
    page = 1
    prev_fingerprint = None
    while page <= 500 and not _sync_stop_flag[0]:
        path = (f"/mypage/earning?earned_from=all&content_type=&year=&month="
                f"&per_page={_PER_PAGE}&page={page}")
        html = get_html(path)
        if html is False:        # transport/HTTP error — not an auth problem
            _sync_log(f"⚠️ PIXTA: стор. {page} недоступна — пропускаю цей синк")
            return "transient"
        if html is None:
            if page == 1:        # first page failed = no/expired session
                _sync_log("⚠️ PIXTA: не залогінений — натисни кнопку «PIXTA» щоб увійти")
                return "needs_login"
            break
        rows = _parse_rows(html)
        if not rows:
            break
        # Guard: some paginators repeat the last page past the end → stop.
        fp = (rows[0]["asset_id"], rows[0]["date"], rows[-1]["asset_id"], len(rows))
        if fp == prev_fingerprint:
            break
        prev_fingerprint = fp
        new_here = 0
        for r in rows:
            if r["price"] <= 0:
                continue
            # _save_record always returns None, so check novelty up front (its
            # date is stored as 19-char "YYYY-MM-DD HH:MM:SS"; we pass T-form
            # which it normalizes identically, so the dedup match is exact).
            norm_date = r["date"].replace("T", " ")
            is_new = not is_already_saved(_STOCK, r["asset_id"], r["price"], norm_date)
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
        _sync_log(f"PIXTA: стор. {page} — {len(rows)} рядків, +{new_here} нових")
        # newest-first: a full page with zero new rows = we've reached the
        # already-collected tail → stop. (page 1 may legitimately be all-known
        # on an up-to-date incremental sync.) The parser may yield 199 of 200
        # rows (one is the header), so do NOT use a strict per_page count to
        # detect the last page — rely on empty/no-new/repeat instead.
        if new_here == 0:
            break
        page += 1
    _sync_log(f"✅ PIXTA: збережено {saved} записів")
    return True
