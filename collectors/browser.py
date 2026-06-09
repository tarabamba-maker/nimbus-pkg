"""
collectors/browser.py — Playwright browser helpers shared by all stock collectors.

Moved here from main.py (logic unchanged — only location changed):
  - _STEALTH_JS
  - _apply_stealth
  - _LOGIN_SIGNALS, _is_login_url
  - _open_browser_context
  - _do_login_flow_global
"""

import os
import sys

from sync_state import _sync_log

IS_MAC = sys.platform == 'darwin'
IS_WIN = sys.platform == 'win32'

# ── Stealth JS ────────────────────────────────────────────────────────────────

_STEALTH_JS = """
    // Hide webdriver flag
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    // Realistic plugin shape (not just an array of numbers)
    Object.defineProperty(navigator, 'plugins', {get: () => {
        return [
            {name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
            {name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: ''},
            {name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: ''},
            {name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: ''},
            {name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: ''},
        ];
    }});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
    Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
    Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
    Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
    // chrome object
    window.chrome = {
        runtime: {},
        loadTimes: function() {},
        csi: function() {},
        app: {}
    };
    // Permissions API spoof
    if (navigator.permissions && navigator.permissions.query) {
        const origQuery = navigator.permissions.query;
        navigator.permissions.query = (p) => p.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : origQuery(p);
    }
    // WebGL vendor/renderer (real Mac M1/Intel values)
    try {
        const getParam = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return 'Apple Inc.';                      // UNMASKED_VENDOR_WEBGL
            if (p === 37446) return 'Apple M1';                        // UNMASKED_RENDERER_WEBGL
            return getParam.call(this, p);
        };
    } catch (e) {}
    // Hide CDP traces in console
    try {
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
    } catch (e) {}
"""


def _apply_stealth(ctx, real_chrome=False):
    # _STEALTH_JS spoofs navigator.platform=MacIntel + Apple WebGL to match the
    # macOS UA on bundled Chromium. On Windows/Linux we drive REAL system Chrome,
    # which is already consistent and trusted — injecting Mac signals there makes
    # the fingerprint mismatch the real platform (Win32 UA vs MacIntel) and trips
    # DataDome instantly. So apply stealth on macOS only.
    #
    # real_chrome=True means we're driving the user's REAL system Chrome via
    # channel='chrome' (e.g. Shutterstock) — even on macOS. That browser already
    # has a clean, trusted fingerprint; injecting stealth ON TOP of it creates the
    # same mismatch Windows would get and gets DataDome to flag/poison the session.
    # This is why Windows (always real Chrome) never applies stealth. Skip it here
    # too so the Mac real-Chrome path behaves exactly like the Windows one.
    if not IS_MAC or real_chrome:
        return
    ctx.add_init_script(_STEALTH_JS)


# ── Login detection ───────────────────────────────────────────────────────────

_LOGIN_SIGNALS = ["auth", "sign-in", "login", "signin", "ims-na1", "sign_in"]

def _is_login_url(url_str):
    u = url_str.lower()
    # "author.envato.com" CONTAINS "auth" but is NOT a login page. Without this
    # strip, _is_login_url() returns True for every Envato author URL → the
    # collector thinks the (already valid) session expired, pops a login window,
    # then bails with "not logged in". Drop the "author." subdomain before matching.
    u = u.replace("author.", "")
    return any(x in u for x in _LOGIN_SIGNALS)


# ── Browser context ───────────────────────────────────────────────────────────

def _open_browser_context(p, profile_dir, headless, off_screen=False, channel=None):
    """Open a Playwright persistent browser context.
    channel='chrome' uses the system-installed Chrome instead of bundled Chromium
    — reduces DataDome detection on Shutterstock. Falls back to chromium if
    Chrome binary isn't found."""
    extra = ["--window-position=0,2000", "--window-size=1280,900"] if off_screen else []
    kwargs = dict(
        user_data_dir=profile_dir,
        headless=headless,
        no_viewport=True,
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"),
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--disable-features=IsolateOrigins,site-per-process",
        ] + extra,
        # Strip Playwright's automation flags that DataDome fingerprints on
        ignore_default_args=["--enable-automation", "--enable-blink-features=IdleDetection"],
    )
    # Will we drive the user's REAL system Chrome (clean, trusted fingerprint)?
    # — always on Windows, and on macOS whenever channel='chrome' resolves to the
    # installed Chrome.app below.
    _mac_chrome = (channel and IS_MAC and os.path.exists(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    use_real_chrome = IS_WIN or bool(_mac_chrome)
    # The hardcoded UA above is a macOS string for BUNDLED Chromium. Real Chrome
    # already sends its own correct, consistent UA — spoofing it on top mismatches
    # the platform signals (a DataDome red flag), so drop the override whenever we
    # drive real Chrome (Windows always, macOS for channel='chrome' stocks).
    if use_real_chrome or not IS_MAC:
        kwargs.pop('user_agent', None)
    if IS_WIN:
        # On Windows ALWAYS drive the user's real system Chrome. This (a) gets
        # past DataDome, and (b) means we never need Playwright's bundled Chromium
        # — so the packaged app ships only Python + deps, not a ~150 MB browser.
        # System Chrome is a hard requirement anyway (the cookie/login flow needs
        # it). Playwright locates it via channel='chrome'.
        kwargs['channel'] = 'chrome'
    elif channel:
        if IS_MAC:
            chrome_app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            if os.path.exists(chrome_app):
                kwargs['channel'] = channel
        else:
            kwargs['channel'] = channel
    return p.chromium.launch_persistent_context(**kwargs)


def _do_login_flow_global(p, profile_dir, target_url, stock_label, wait_cond):
    """Відкриває видимий браузер, чекає поки юзер залогіниться і закриє вікно."""
    _sync_log(f"🔒 {stock_label}: потрібна авторизація — відкриваю браузер...")
    # Shutterstock: use real Chrome for login too (matches collector engine)
    channel = "chrome" if stock_label == "Shutterstock" else None
    vis = _open_browser_context(p, profile_dir, headless=False, channel=channel)
    _apply_stealth(vis, real_chrome=(channel == "chrome"))
    vp = vis.pages[0] if vis.pages else vis.new_page()
    try:
        vp.goto(target_url, wait_until=wait_cond, timeout=90000)
    except Exception:
        pass
    _sync_log(f"👤 {stock_label}: залогуйся і ЗАКРИЙ ВІКНО БРАУЗЕРА — збір продовжиться автоматично (макс 10 хв)")
    try:
        # Capture cookies to the cache while open (decrypted by Playwright) — the only
        # reliable cookie source on macOS. 10-min cap so a walk-away still recovers.
        from cookies import _wait_close_capturing
        _wait_close_capturing(vis, timeout_ms=600000)
    except Exception:
        _sync_log(f"⏱ {stock_label}: 10-хв timeout — закриваю браузер примусово")
    finally:
        try: vis.close()
        except Exception: pass
    _sync_log(f"✅ {stock_label}: браузер закрито, продовжую збір...")
