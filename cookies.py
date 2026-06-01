"""
cookies.py — Browser cookie helpers (macOS Safari + Windows Chrome).

Moved here from main.py (logic unchanged — only location changed):
  - _is_chrome_running, _chrome_cookies_path
  - _decrypt_chrome_cookie_db, _load_chrome_cookies_windows
  - _load_appprofile_cookies_windows, _load_browser_cookies
  - _parse_safari_binarycookies
  - _inject_cookies_via_playwright
  - _STOCK_COOKIE_DOMAINS, _import_cookies_for_stock
  - _is_safari_running, _detect_default_browser
  - _STOCK_LOGIN_URLS, _stock_profile_dir, _clear_profile_locks
  - _windows_browser_login
"""

import os
import sqlite3
import sys
import time

IS_MAC = sys.platform == 'darwin'
IS_WIN = sys.platform == 'win32'

_SUBPROC_NOWINDOW = (
    {'creationflags': 0x0800_0000} if IS_WIN else {}
)

_BASE_DIR = os.environ.get(
    "STOCK_DATA_DIR",
    os.path.dirname(os.path.abspath(__file__))
)

from utils import _dpapi_unprotect
from collectors.browser import _open_browser_context, _apply_stealth
from sync_state import _sync_log


def _app_log_lazy(msg):
    try:
        from main import _app_log
        _app_log_lazy(msg)
    except Exception:
        print(msg)


def _is_chrome_running():
    """Returns True if Google Chrome is currently running (would lock cookies file).
    Cross-platform: pgrep on Unix, tasklist on Windows."""
    import subprocess
    try:
        if IS_WIN:
            r = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq chrome.exe', '/NH'],
                capture_output=True, timeout=5, text=True, **_SUBPROC_NOWINDOW)
            return 'chrome.exe' in (r.stdout or '').lower()
        r = subprocess.run(['pgrep', '-f', 'Google Chrome'],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

def _chrome_cookies_path():
    """Path to Chrome's cookies SQLite. Schema location differs per OS:
       - macOS: ~/Library/Application Support/Google/Chrome/Default/Cookies
       - Windows: %LOCALAPPDATA%\\Google\\Chrome\\User Data\\Default\\Network\\Cookies
                  (older Chromes: ...\\Default\\Cookies)
       Returns the first existing path, or '' if none."""
    candidates = []
    if IS_WIN:
        local = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/AppData/Local')
        candidates = [
            os.path.join(local, 'Google', 'Chrome', 'User Data', 'Default', 'Network', 'Cookies'),
            os.path.join(local, 'Google', 'Chrome', 'User Data', 'Default', 'Cookies'),
        ]
    else:
        candidates = [
            os.path.expanduser("~/Library/Application Support/Google/Chrome/Default/Cookies"),
            os.path.expanduser("~/.config/google-chrome/Default/Cookies"),  # Linux
        ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return ''


# ═══════════════════════════════════════════════════════════
# Cross-platform browser cookies
# ═══════════════════════════════════════════════════════════
# Collectors authenticate to stock sites by reading session cookies set by
# the user in their native browser. On macOS we parse Safari's
# Cookies.binarycookies directly. On Windows there's no Safari — we read
# Chrome's encrypted cookie store via the `browser_cookie3` library.
#
# Unified return shape: list of {'name', 'value', 'domain'} dicts. Existing
# Mac collectors filter by domain in-place — Windows path returns ALL cookies
# (filtering happens at call site).

# _dpapi_unprotect → utils.py


def _decrypt_chrome_cookie_db(cookie_db):
    """Decrypt one Chrome cookie SQLite directly. Handles v10/v11 (AES-256-GCM
    under a DPAPI-wrapped key) and legacy raw-DPAPI values. v20 (App-Bound
    Encrypted, Chrome 127+) cookies can't be decrypted outside Chrome itself and
    are skipped (browser_cookie3 instead raises on the first one, aborting the
    whole read). Finds the matching 'Local State' by walking up from cookie_db.
    Returns list of {'name','value','domain'}."""
    import json, base64, sqlite3, shutil, tempfile

    if not cookie_db or not os.path.exists(cookie_db):
        return []

    # Locate Local State (holds the encrypted master key) above the DB.
    local_state = ''
    d = os.path.dirname(cookie_db)
    for _ in range(5):
        cand = os.path.join(d, 'Local State')
        if os.path.exists(cand):
            local_state = cand
            break
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd

    aes_key = None
    try:
        ls = json.load(open(local_state, encoding='utf-8'))
        enc_key = base64.b64decode(ls['os_crypt']['encrypted_key'])
        if enc_key[:5] == b'DPAPI':
            aes_key = _dpapi_unprotect(enc_key[5:])
    except Exception as e:
        _app_log_lazy(f"⚠️ Chrome master key load failed ({cookie_db}): {e}")

    # Chrome keeps a lock on the live DB — read from a copy.
    tmp = tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False).name
    try:
        shutil.copyfile(cookie_db, tmp)
        con = sqlite3.connect(tmp)
        rows = con.execute(
            'SELECT host_key, name, encrypted_value, value FROM cookies').fetchall()
        con.close()
    except Exception as e:
        _app_log_lazy(f"⚠️ Chrome cookie DB read failed ({cookie_db}): {e}")
        return []
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass

    from Cryptodome.Cipher import AES
    out, n_v20, n_fail = [], 0, 0
    for host, name, ev, plain in rows:
        ev = bytes(ev) if ev else b''
        try:
            if not ev:
                val = plain or ''
            elif ev[:3] in (b'v10', b'v11'):
                if aes_key is None:
                    n_fail += 1
                    continue
                dec = AES.new(aes_key, AES.MODE_GCM, nonce=ev[3:15]) \
                    .decrypt_and_verify(ev[15:-16], ev[-16:])
                # Chrome 124+ prepends a 32-byte SHA-256(domain) integrity hash.
                val = dec[32:].decode('utf-8', 'replace')
            elif ev[:3] == b'v20':
                n_v20 += 1
                continue
            else:
                val = _dpapi_unprotect(ev).decode('utf-8', 'replace')
        except Exception:
            n_fail += 1
            continue
        out.append({'name': name, 'value': val, 'domain': host.lstrip('.')})

    if n_v20:
        _app_log_lazy(f"ℹ️ {os.path.basename(os.path.dirname(os.path.dirname(cookie_db)))}: "
                 f"{n_v20} v20 cookies skipped, {len(out)} readable")
    return out


def _load_chrome_cookies_windows():
    """Native Chrome cookies (rarely useful on Chrome 127+ — they're v20). Kept
    for completeness; variant 3 reads the app's own profiles instead."""
    return _decrypt_chrome_cookie_db(_chrome_cookies_path())


def _load_appprofile_cookies_windows():
    """Variant 3: aggregate cookies from the app's own Chrome profiles, where the
    user logged in via real Chrome (`_windows_browser_login`). Automation-launched
    Chrome keeps App-Bound Encryption OFF, so those cookies are plain v10 and
    decrypt directly — no Playwright needed. Each profile holds only its stock's
    cookies; callers filter by domain, so merging is safe."""
    import glob
    roots = {_BASE_DIR, os.getcwd()}
    profiles = set()
    for r in roots:
        profiles.update(glob.glob(os.path.join(r, 'chrome_profile*')))
        profiles.update(glob.glob(os.path.join(r, '*_profile')))
    seen, out = set(), []
    for prof in profiles:
        for sub in (os.path.join(prof, 'Default', 'Network', 'Cookies'),
                    os.path.join(prof, 'Default', 'Cookies')):
            if os.path.exists(sub):
                for c in _decrypt_chrome_cookie_db(sub):
                    k = (c['domain'], c['name'])
                    if k not in seen:
                        seen.add(k)
                        out.append(c)
                break
    return out

def _load_browser_cookies():
    """OS-agnostic: returns all browser cookies as list of dicts.
    On macOS: Safari binarycookies. On Windows: the app's own Chrome login
    profiles (variant 3), since Chrome 127+ App-Bound Encryption makes the
    user's native Chrome cookies unreadable from outside Chrome."""
    if IS_MAC:
        path = os.path.expanduser(
            "~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies")
        if not os.path.exists(path):
            return []
        try:
            return _parse_safari_binarycookies(path)
        except Exception as e:
            _app_log_lazy(f"⚠️ Safari cookies parse failed: {e}")
            return []
    if IS_WIN:
        return _load_appprofile_cookies_windows()
    return []


def _parse_safari_binarycookies(filepath):
    """Parse Apple's binarycookies format. Returns list of cookie dicts."""
    import struct
    with open(filepath, 'rb') as f:
        data = f.read()
    if data[:4] != b'cook':
        raise ValueError('Not a Safari Cookies.binarycookies file')

    num_pages = struct.unpack('>I', data[4:8])[0]
    page_sizes = []
    off = 8
    for _ in range(num_pages):
        page_sizes.append(struct.unpack('>I', data[off:off+4])[0])
        off += 4
    # Skip checksum (uint32 BE) + footer (uint64 BE)
    cookies = []
    page_start = off + 4  # there's also a checksum block but offset may differ
    # The robust way: just find each page by its magic 0x00000100 starting from off
    cur = off
    for page_size in page_sizes:
        page = data[cur:cur+page_size]
        cur += page_size
        # Page magic is exactly 4 bytes 00 00 01 00 (NOT a number — endianness-agnostic byte sequence)
        if len(page) < 4 or page[:4] != b'\x00\x00\x01\x00':
            continue
        n_cookies = struct.unpack('<I', page[4:8])[0]
        offsets = [struct.unpack('<I', page[8+i*4:12+i*4])[0] for i in range(n_cookies)]
        for co in offsets:
            try:
                csize = struct.unpack('<I', page[co:co+4])[0]
                cd = page[co:co+csize]
                flags = struct.unpack('<I', cd[8:12])[0]
                url_off  = struct.unpack('<I', cd[16:20])[0]
                name_off = struct.unpack('<I', cd[20:24])[0]
                path_off = struct.unpack('<I', cd[24:28])[0]
                val_off  = struct.unpack('<I', cd[28:32])[0]
                expiration = struct.unpack('<d', cd[40:48])[0]
                def _rs(o):
                    end = cd.index(b'\x00', o)
                    return cd[o:end].decode('utf-8', errors='replace')
                cookies.append({
                    'domain': _rs(url_off),
                    'name':   _rs(name_off),
                    'value':  _rs(val_off),
                    'path':   _rs(path_off) or '/',
                    'expires': (expiration + 978307200) if expiration > 0 else -1,
                    'secure':   bool(flags & 1),
                    'httpOnly': bool(flags & 4),
                })
            except Exception:
                continue
    return cookies


def _inject_cookies_via_playwright(stock_name, cookies):
    """Open a brief headless Playwright context with the stock profile and
    inject given cookies via the standard API. Cookies persist in profile."""
    from playwright.sync_api import sync_playwright as _spw
    safe = stock_name.replace(" ", "_")
    stock_profile = os.path.join(_BASE_DIR, f"chrome_profile_{safe}")
    os.makedirs(stock_profile, exist_ok=True)
    # Normalize cookies for Playwright API
    pw_cookies = []
    for c in cookies:
        d = c.get('domain', '')
        if not d:
            continue
        # Playwright requires either url= or (domain= AND path=)
        if not d.startswith('.') and '.' not in d.lstrip('.'):
            continue
        pw_cookies.append({
            'name':   c.get('name', ''),
            'value':  c.get('value', ''),
            'domain': d,
            'path':   c.get('path', '/') or '/',
            'expires': float(c.get('expires', -1)),
            'secure': bool(c.get('secure', False)),
            'httpOnly': bool(c.get('httpOnly', False)),
            'sameSite': c.get('sameSite', 'Lax') if c.get('sameSite') in ('Strict','Lax','None') else 'Lax',
        })
    if not pw_cookies:
        return 0
    try:
        with _spw() as p:
            ctx = _open_browser_context(p, stock_profile, headless=True,
                                        channel='chrome' if stock_name == 'Shutterstock' else None)
            try:
                ctx.add_cookies(pw_cookies)
            finally:
                ctx.close()
        return len(pw_cookies)
    except Exception as e:
        _app_log_lazy(f"[cookies] inject {stock_name} failed: {e}")
        return 0


# Domain patterns per stock for cookie filtering when importing from native Chrome
_STOCK_COOKIE_DOMAINS = {
    "Adobe Stock":   ["adobe.com", "stock.adobe.com", "contributor.stock.adobe.com",
                      "ims-na1.adobelogin.com"],
    "Shutterstock":  ["shutterstock.com", "submit.shutterstock.com"],
    "Getty Images":  ["gettyimages.com", "esp.gettyimages.com",
                      "accountmanagement.gettyimages.com"],
    "Depositphotos": ["depositphotos.com"],
    "Microstock+":   ["microstock.plus"],
}


def _import_cookies_for_stock(stock_name, source_cookies_db):
    """Copy native Chrome cookies for one stock's domains into its Playwright profile.
    Preserves encrypted_value blob — Playwright Chrome decrypts via same macOS
    Keychain key (same user) so auth survives."""
    safe = stock_name.replace(" ", "_")
    stock_profile = os.path.join(_BASE_DIR, f"chrome_profile_{safe}")
    dest_dir = os.path.join(stock_profile, "Default")
    os.makedirs(dest_dir, exist_ok=True)
    dest_db = os.path.join(dest_dir, "Cookies")

    domains = _STOCK_COOKIE_DOMAINS.get(stock_name, [])
    if not domains:
        return {'stock': stock_name, 'imported': 0, 'msg': 'no domain pattern'}

    # Build LIKE clauses
    where = " OR ".join(["host_key LIKE ?"] * len(domains))
    params = [f"%{d}%" for d in domains]

    try:
        src = sqlite3.connect(f"file:{source_cookies_db}?mode=ro", uri=True, timeout=5)
        rows = src.execute(
            f"SELECT * FROM cookies WHERE {where}", params).fetchall()
        cols = [d[0] for d in src.execute(f"SELECT * FROM cookies WHERE {where} LIMIT 0", params).description]
        src.close()
    except Exception as e:
        return {'stock': stock_name, 'imported': 0, 'error': f'read source: {e}'}

    if not rows:
        return {'stock': stock_name, 'imported': 0, 'msg': 'no matching cookies in native Chrome'}

    # Init dest schema by copying file if missing, or open existing
    try:
        if not os.path.exists(dest_db):
            import shutil as _sh
            _sh.copy2(source_cookies_db, dest_db)
            # Clear all cookies in copied DB then re-insert filtered set
            with sqlite3.connect(dest_db, timeout=10) as c:
                c.execute("DELETE FROM cookies")
                c.commit()
        # Insert filtered rows
        placeholders = ",".join(["?"] * len(cols))
        with sqlite3.connect(dest_db, timeout=10) as c:
            # Remove existing matching domains so we don't duplicate
            c.execute(f"DELETE FROM cookies WHERE {where}", params)
            c.executemany(
                f"INSERT INTO cookies ({','.join(cols)}) VALUES ({placeholders})",
                rows)
            c.commit()
    except Exception as e:
        return {'stock': stock_name, 'imported': 0, 'error': f'write dest: {e}'}

    return {'stock': stock_name, 'imported': len(rows)}


def _is_safari_running():
    """Returns True if Safari is currently running."""
    import subprocess
    try:
        r = subprocess.run(['pgrep', '-x', 'Safari'], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _detect_default_browser():
    """Detects macOS default browser via LaunchServices plist. Returns one of
    'safari', 'chrome', 'edge', or None."""
    import plistlib
    plist_path = os.path.expanduser(
        '~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist')
    if not os.path.exists(plist_path):
        return None
    try:
        with open(plist_path, 'rb') as f:
            data = plistlib.load(f)
    except Exception:
        return None
    bundle = None
    for h in data.get('LSHandlers', []):
        if h.get('LSHandlerURLScheme') == 'http':
            bundle = (h.get('LSHandlerRoleAll') or '').lower()
            break
    if not bundle:
        return None
    if 'safari' in bundle:        return 'safari'
    if 'chrome' in bundle:        return 'chrome'
    if 'edgemac' in bundle:       return 'edge'
    if 'firefox' in bundle:       return 'firefox'
    if 'thebrowser' in bundle:    return 'arc'
    return None


# Login landing pages per stock — same URLs the sync login flow uses.
_STOCK_LOGIN_URLS = {
    "Adobe Stock":   "https://contributor.stock.adobe.com/en/insights/sales-earnings",
    "Shutterstock":  "https://submit.shutterstock.com/earnings",
    "Depositphotos": "https://depositphotos.com/account/sales-history.html",
    "Getty Images":  "https://accountmanagement.gettyimages.com/Reports/Export",
    "Microstock+":   "https://microstock.plus/myfiles",
}


def _stock_profile_dir(stock_name):
    """Profile dir a collector reads for this stock (matches the sync loop)."""
    if stock_name == "Getty Images":
        return os.path.abspath("getty_profile")
    return os.path.abspath(f"chrome_profile_{stock_name.replace(' ', '_')}")


def _clear_profile_locks(profile_dir):
    for _lf in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        _lp = os.path.join(profile_dir, _lf)
        if os.path.exists(_lp):
            try:
                os.remove(_lp)
            except Exception:
                pass


def _windows_browser_login(stocks):
    """Variant 3 (Windows): Chrome 127+ App-Bound Encryption makes the user's
    native Chrome cookies undecryptable outside Chrome. Instead, open ONE real
    (non-automation) Chrome window with one tab per stock on the app's seed
    profile so the user logs into everything at once, then closes the window.
    The sessions persist in the profile and are read back by _load_browser_cookies
    (via the profile aggregator) — nothing else to press. Blocks until the window
    is closed (max 10 min). `stocks` may be a single-item list for re-login of one
    expired stock."""
    from playwright.sync_api import sync_playwright as _spw
    login_prof = os.path.abspath("chrome_profile")
    os.makedirs(login_prof, exist_ok=True)
    _clear_profile_locks(login_prof)

    targets = [(s, _STOCK_LOGIN_URLS[s]) for s in stocks if s in _STOCK_LOGIN_URLS]
    if not targets:
        return [{'stock': s, 'imported': 0, 'msg': 'no login url'} for s in stocks]

    opened = []
    try:
        with _spw() as p:
            vis = _open_browser_context(p, login_prof, headless=False, channel="chrome")
            _apply_stealth(vis)
            # Shutterstock's DataDome rejects a stale/flagged token outright (it
            # serves a blank block page instead of the login form). Clear its
            # cookies first so it issues a fresh, clean token. Other stocks just
            # redirect to a normal login when stale, so leave their cookies.
            if any(n == "Shutterstock" for n, _ in targets):
                try:
                    for d in set(c.get('domain', '') for c in vis.cookies()
                                 if 'shutterstock' in c.get('domain', '')):
                        try:
                            vis.clear_cookies(domain=d)
                        except Exception:
                            pass
                except Exception:
                    pass
            # Shutterstock's DataDome is the most aggressive and flags request
            # bursts — open it first, then stagger the rest so 5 tabs don't all
            # hit at once (which gets Shutterstock challenged even on real Chrome).
            targets.sort(key=lambda t: 0 if t[0] == "Shutterstock" else 1)
            for i, (name, url) in enumerate(targets):
                pg = vis.pages[0] if (i == 0 and vis.pages) else vis.new_page()
                try:
                    pg.goto(url, timeout=90000)
                except Exception:
                    pass
                opened.append(name)
                time.sleep(3 if name == "Shutterstock" else 1.5)
            _sync_log("👤 Залогінься у КОЖНІЙ вкладці (" + ", ".join(opened) +
                      ") і ЗАКРИЙ ВІКНО — далі все підхопиться автоматично (макс 10 хв)")
            try:
                vis.wait_for_event("close", timeout=600000)
            except Exception:
                _sync_log("⏱ 10-хв timeout — закриваю вікно логіну")
            finally:
                try:
                    vis.close()
                except Exception:
                    pass
    except Exception as e:
        _app_log_lazy(f"[win-login] failed: {e}")
        return [{'stock': s, 'imported': 0, 'error': str(e)} for s in opened or stocks]

    return [{'stock': s, 'imported': 1, 'msg': 'login window closed'} for s in opened]

