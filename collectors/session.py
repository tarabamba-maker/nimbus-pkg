"""
collectors/session.py — THE ONE global collection mechanism (macOS == Windows scheme).

The whole pipeline, shared by every stock:
  1. LOGIN   — native Chrome (a real human session, NO Playwright/CDP) writes the
               stock's cookies into an app-owned profile with the real OS key
               (macOS Keychain / Windows DPAPI). See cookies._native_chrome_login.
  2. IMPORT  — decrypt that profile's cookie DB (the app owns it → has the key).
  3. COLLECT — direct HTTPS with those cookies. No browser at collection time.

A collector just calls `stock_session(domains)` to get a ready requests.Session and
runs its normal parse loop. Login/retry is centralised in the orchestrator
(direct → on block, native login → retry once), so adding a stock is: write a
`*_collect_direct()` using stock_session, register it. No per-stock browser code.

Why this beats the old Playwright path: Playwright drives Chrome over CDP, which
anti-bots (PerimeterX "Press & Hold" on Dreamstime, DataDome) detect — and on
macOS Playwright's --use-mock-keychain made the profile cookies undecryptable,
which forced the whole _pw_cookies.json detour. A native login sidesteps both.
"""

import requests

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_BASE_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def stock_session(domains, referer=None, xhr=False, accept=None):
    """Build a requests.Session preloaded with the stock's cookies (imported from
    the native Chrome login) + realistic browser headers.

    domains: substrings to match a cookie's host (e.g. ["dreamstime.com"]).
    referer/xhr/accept: per-stock header tweaks (some XHR APIs need them).
    Returns None if no cookies are present for those domains (→ caller triggers a
    native login)."""
    from cookies import _load_browser_cookies
    s = requests.Session()
    s.headers.update(_BASE_HEADERS)
    if accept:
        s.headers["Accept"] = accept
    if referer:
        s.headers["Referer"] = referer
    if xhr:
        s.headers["X-Requested-With"] = "XMLHttpRequest"
    n = 0
    for c in _load_browser_cookies():
        dom = (c.get("domain") or "").lstrip(".")
        if any(d in dom for d in domains):
            try:
                s.cookies.set(c["name"], c["value"], domain="." + dom)
                n += 1
            except Exception:
                pass
    return s if n else None
