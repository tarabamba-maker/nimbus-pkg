"""
collectors/alamy.py — Alamy (alamy.com) contributor earnings collector.

DATA MODEL (confirmed via Inspector, 2026-06-13):
  Host: www.alamy.com. Auth: cookie session (shared chrome_profile). The
  contributor reports are ASP.NET WebForms pages (Reports.aspx) — NOT a JSON
  API and NOT in-page-fetchable: pagination + page-size are __doPostBack
  postbacks, so we DRIVE the rendered page (same exception as Depositphotos).

  TWO reports, joined on the Alamy image ref (e.g. "2GE3ADA"):
    • Rep=0  Sales history  — per-sale rows WITH a watermark-free thumbnail
        (http://c7.alamy.com/thumbs/<n>/<GUID>/<REF>.jpg) and the GROSS sale $.
        Used ONLY for the {ref: thumb} map.
    • Rep=1  Balance of account — per-image LEDGER: a "Sale" credit row plus one
        or more "Commission/charge" debit rows, same date. The contributor NET =
        Σ Sale credits − Σ commission debits, per (ref, date). This is the real
        earning (Rep_0's amount is gross — verified: 2H4X17C gross $37.34 →
        commissions $32.86 → net $4.48). Used for the money.

  URL: /alamycontributorreports/Reports.aspx?Rep={0|1}
        &sdate=01-Jan-2000&edate=<today DD-Mon-YYYY>&r2checked=False&drpval=2000&netType=1
  Page size: <select id="drpPagesize"> 20/50/100 → __doPostBack. We pick 100.
  Pages: "Page X of Y" + <a id="lnkbtnNext">. Click Next until X==Y.

  ⚠️ Date-only ledger (no per-sale timestamp), so two same-day sales of one image
  collapse into one (ref,date) net record — same limitation as Depositphotos.

STATE: none — relies on _save_record's day+price dedup for date-only sales.
"""

import re
from datetime import date, datetime

from db import is_already_saved
from sync_state import _app_log, _save_record, _sync_log, _sync_stop_flag
from collectors.browser import _is_login_url

_BASE  = "https://www.alamy.com"
_STOCK = "Alamy"
_REF   = re.compile(r'\b(2[A-Z0-9]{6})\b')   # this account's refs all start "2…"


def _report_url(rep: int) -> str:
    edate = date.today().strftime("%d-%b-%Y")
    return (f"{_BASE}/alamycontributorreports/Reports.aspx?Rep={rep}"
            f"&sdate=01-Jan-2000&edate={edate}&r2checked=False&drpval=2000&netType=1")


def _money(s: str) -> float:
    s = re.sub(r'[^\d.]', '', s or '')
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _norm_date(s: str):
    """'28 February 2024' → '2024-02-28'."""
    s = s.strip()
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return None


# ── page driving ──────────────────────────────────────────────────────────────

def _page_of(html: str):
    m = re.search(r'Page\s+(\d+)\s+of\s+(\d+)', html)
    return (int(m.group(1)), int(m.group(2))) if m else (1, 1)


def _fingerprint(html: str) -> str:
    """First ~3 refs on the page — changes when the page advances."""
    return ",".join(_REF.findall(html)[:3])


def _wait_change(pw_page, prev_fp: str, timeout: float = 40.0, want_page: int = None) -> bool:
    """Poll until the ASP.NET partial postback finished. wait_for_load_state
    returns too early for an UpdatePanel. Prefer the deterministic "Page X of Y"
    increment (want_page); fall back to a content-fingerprint change."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.6)
        try:
            html = pw_page.content()
        except Exception:
            continue
        if want_page is not None:
            cur, _ = _page_of(html)
            if cur == want_page:
                return True
        elif _fingerprint(html) != prev_fp:
            return True
    return False


def _walk_report(pw_page, rep: int, on_html):
    """Navigate Rep, set 100/page, call on_html(html) per page, page to the end.
    Pagination is driven by REAL clicks (the <a href="javascript:__doPostBack(…)">
    runs in the page's own non-strict context — calling __doPostBack from
    page.evaluate() throws on ASP.NET's arguments.callee access)."""
    pw_page.goto(_report_url(rep), wait_until="networkidle", timeout=60000)
    if _is_login_url(pw_page.url):
        return "needs_login"
    # Bump to 100/page (fewer postbacks). The <select> onchange fires __doPostBack.
    if pw_page.query_selector("#drpPagesize"):
        fp = _fingerprint(pw_page.content())
        try:
            pw_page.select_option("#drpPagesize", "100")
            _wait_change(pw_page, fp, timeout=25)
        except Exception as ex:
            _app_log(f"[Alamy] pagesize set failed: {ex}")

    seen_pages = 0
    while not _sync_stop_flag[0] and seen_pages < 500:
        html = pw_page.content()
        on_html(html)
        cur, tot = _page_of(html)
        seen_pages += 1
        if cur >= tot:
            break
        fp = _fingerprint(html)
        try:
            pw_page.click("#lnkbtnNext", timeout=10000)
        except Exception as ex:
            _app_log(f"[Alamy] Rep{rep} next-click failed p{cur}: {ex}")
            break
        if not _wait_change(pw_page, fp, timeout=40, want_page=cur + 1):
            _app_log(f"[Alamy] Rep{rep} pagination stuck at page {cur}/{tot}")
            break
    return True


# ── parsing ───────────────────────────────────────────────────────────────────

def _parse_thumbs(html: str, out: dict):
    """Rep_0 → {ref: thumb_url}. Thumb src has the ref as its filename."""
    for m in re.finditer(
            r'(https?://[^"\']*?/thumbs/\d+/[A-F0-9\-]+/([A-Z0-9]+)\.jpg)', html, re.I):
        url, ref = m.group(1), m.group(2)
        if url.startswith("http://"):
            url = "https://" + url[7:]
        out.setdefault(ref, url)


def _parse_ledger(html: str, sales: dict, comm: dict):
    """Rep_1 → accumulate Σ Sale credits and Σ commission debits per (ref,date)."""
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S):
        cells = [re.sub(r'<[^>]+>', ' ', c).strip()
                 for c in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
        if len(cells) < 6:
            continue
        joined = " ".join(cells)
        refm = _REF.search(joined)
        datem = _norm_date(cells[0])
        if not refm or not datem:
            continue
        ref = refm.group(1)
        key = (ref, datem)
        low = joined.lower()
        nums = [_money(c) for c in cells if re.search(r'\d\.\d{2}', c)]
        if not nums:
            continue
        if "commission" in low or "charge" in low:
            comm[key] = comm.get(key, 0.0) + nums[0]
        elif "sale" in low:
            sales[key] = sales.get(key, 0.0) + nums[0]


# ── main collect ──────────────────────────────────────────────────────────────

def _alamy_form_fields(html: str) -> dict:
    """Extract all <input>/<select> form fields for an ASP.NET WebForms postback."""
    fields = {}
    for m in re.finditer(r'<input[^>]+name="([^"]+)"[^>]*?value="([^"]*)"', html):
        fields[m.group(1)] = m.group(2)
    for sm in re.finditer(r'<select[^>]+name="([^"]+)"[^>]*>(.*?)</select>', html, re.S):
        body = sm.group(2)
        om = (re.search(r'<option[^>]*selected[^>]*value="([^"]*)"', body)
              or re.search(r'value="([^"]*)"[^>]*selected', body))
        if om:
            fields[sm.group(1)] = om.group(1)
    return fields


def _walk_report_direct(sess, rep: int, on_html):
    """Direct ASP.NET postback walk: GET the report, set 100/page, then POST
    __doPostBack('lnkbtnNext') re-using the fresh __VIEWSTATE each time."""
    url = _report_url(rep)
    r = sess.get(url, timeout=60, headers={"Accept": "text/html,*/*;q=0.8"})
    if r.status_code in (401, 403) or _is_login_url(r.url) or "log-in" in r.url:
        return "needs_login"
    html = r.text
    fields = _alamy_form_fields(html)
    if fields.get("drpPagesize") and fields["drpPagesize"] != "100":
        fields["drpPagesize"] = "100"
        fields["__EVENTTARGET"] = "drpPagesize"; fields["__EVENTARGUMENT"] = ""
        try:
            html = sess.post(url, data=fields, timeout=60).text
        except Exception as ex:
            _app_log(f"[Alamy] direct pagesize: {ex}")
    seen = 0
    while seen < 500 and not _sync_stop_flag[0]:
        on_html(html)
        seen += 1
        cur, tot = _page_of(html)
        if cur >= tot and tot > 1:
            break
        fp = _fingerprint(html)
        fields = _alamy_form_fields(html)
        fields["__EVENTTARGET"] = "lnkbtnNext"; fields["__EVENTARGUMENT"] = ""
        try:
            nhtml = sess.post(url, data=fields, timeout=60).text
        except Exception as ex:
            _app_log(f"[Alamy] direct next p{cur}: {ex}")
            break
        if _fingerprint(nhtml) == fp or not _REF.search(nhtml):
            break
        html = nhtml
    return True


def _alamy_collect_direct():
    """Direct-HTTPS collect with native-login cookies (no Playwright)."""
    from collectors.session import stock_session
    sess = stock_session(["alamy.com"], accept="text/html")
    if sess is None:
        _sync_log("⚠️ Alamy direct: нема cookies — потрібен нативний логін")
        return False
    res = _alamy_run(lambda rep, cb: _walk_report_direct(sess, rep, cb))
    return False if res == "needs_login" else True


def _alamy_collect(pw_page):
    """Playwright fallback (drives the rendered ASP.NET page)."""
    _sync_log("📊 Alamy: перевіряємо сесію…")
    pw_page.goto(f"{_BASE}/alamycontributorreports/Reports.aspx?Rep=0",
                 wait_until="networkidle", timeout=60000)
    if _is_login_url(pw_page.url) or "log-in" in pw_page.url:
        _sync_log("⚠️ Alamy: не залогінений — натисни кнопку «Alamy» щоб увійти")
        return "needs_login"
    return _alamy_run(lambda rep, cb: _walk_report(pw_page, rep, cb))


def _alamy_run(walk):
    """Core: walk(rep, on_html) yields each report page's HTML. Rep_1 → net
    ledger (Sale − commission), Rep_0 → thumbs. Returns True / 'needs_login'.

    Ledger goes FIRST so the (slow, paginated) Rep_0 thumbs walk can be skipped
    entirely when every ref in the ledger already has a cached thumbnail —
    which is every sync except when a never-before-sold image sells."""
    from image_utils import load_img_async
    from app_globals import CACHE_DIR
    import os as _os
    _sync_log("✅ Alamy: сесія активна")

    sales: dict = {}
    comm: dict = {}
    if walk(1, lambda h: _parse_ledger(h, sales, comm)) == "needs_login":
        return "needs_login"

    refs = {ref for ref, _d in sales}
    missing = {r for r in refs
               if not _os.path.exists(_os.path.join(CACHE_DIR, f"{r}.jpg"))}
    thumbs: dict = {}
    if missing:
        if walk(0, lambda h: _parse_thumbs(h, thumbs)) == "needs_login":
            return "needs_login"
        _sync_log(f"Alamy: {len(thumbs)} сабнейлів зібрано (Rep_0, бракувало {len(missing)})")
    else:
        _sync_log(f"Alamy: всі {len(refs)} refs уже мають кешовані сабнейли — Rep_0 пропущено")

    saved = 0
    for key, gross in sales.items():
        if _sync_stop_flag[0]:
            break
        ref, d = key
        net = round(gross - comm.get(key, 0.0), 2)
        if net <= 0:
            continue
        thumb = thumbs.get(ref, "")
        is_new = not is_already_saved(_STOCK, ref, net, d)
        _save_record({
            "stock":     _STOCK,
            "asset_id":  ref,
            "price":     net,
            "date":      d,
            "thumb_url": thumb,
        })
        if is_new:
            saved += 1
            if thumb:
                load_img_async(ref, thumb, None, is_adobe=False, stock=_STOCK)
    _sync_log(f"✅ Alamy: збережено {saved} записів (net), {len(sales)} продажів у леджері")
    return True
