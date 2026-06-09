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

# ── Playwright cookie cache (macOS) ──────────────────────────────────────────
# On macOS the app's Chrome/Chromium profiles encrypt cookies with a key we can't
# recover (Playwright launches Chrome with --use-mock-keychain/--password-store=
# basic, so neither the 'Chrome Safe Storage' nor 'Chromium Safe Storage' Keychain
# key decrypts them). Instead we snapshot cookies straight from the Playwright
# context during login — context.cookies() returns them ALREADY DECRYPTED (httpOnly
# included) — and persist to this plain JSON. Collectors then read it via
# _load_browser_cookies(). This is the macOS equivalent of the Windows
# app-profile-cookie path and is why "no browser cookies" stops happening.
_PW_COOKIE_CACHE = os.path.join(_BASE_DIR, "recipes", "_pw_cookies.json")


def _save_pw_cookie_cache(pw_cookies):
    """Write Playwright-decrypted cookies (list of dicts from context.cookies()) to
    the plain JSON cache. Merges by (domain, name) so logging into one stock doesn't
    wipe another stock's still-valid cookies."""
    import json as _json
    try:
        os.makedirs(os.path.dirname(_PW_COOKIE_CACHE), exist_ok=True)
        merged = {}
        for c in _load_pw_cookie_cache():           # keep existing
            merged[(c.get('domain', ''), c.get('name', ''))] = c
        for c in pw_cookies:                          # overlay fresh
            d = (c.get('domain') or '').lstrip('.')
            if not c.get('name'):
                continue
            merged[(d, c.get('name'))] = {
                'name': c.get('name', ''), 'value': c.get('value', ''),
                'domain': d, 'path': c.get('path', '/') or '/',
                'expires': c.get('expires', -1),
                'secure': bool(c.get('secure', False)),
                'httpOnly': bool(c.get('httpOnly', False)),
            }
        out = list(merged.values())
        with open(_PW_COOKIE_CACHE, 'w') as f:
            _json.dump(out, f)
        return len(out)
    except Exception as e:
        _app_log(f"[cookies] pw cache write failed: {e}")
        return 0


def _load_pw_cookie_cache():
    import json as _json
    try:
        with open(_PW_COOKIE_CACHE) as f:
            data = _json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _capture_pw_cookies(context):
    """Snapshot the live Playwright context's cookies into the cache. Safe to call
    repeatedly; returns count written (0 if the context is already closed)."""
    try:
        cks = context.cookies()
    except Exception:
        return 0
    return _save_pw_cookie_cache(cks) if cks else 0


def _wait_close_capturing(context, timeout_ms=600000):
    """Wait for the login window to close, snapshotting cookies to the cache every
    few seconds so the post-login session is captured even though we never know
    exactly when the user finishes. Replaces a bare wait_for_event('close')."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        n = _capture_pw_cookies(context)
        if n == 0:        # context closed (cookies() threw) → done
            break
        try:
            context.wait_for_event("close", timeout=2500)
            break          # closed within the slice
        except Exception:
            continue        # timeout slice elapsed → loop & re-snapshot
    _capture_pw_cookies(context)   # best-effort final snapshot

from utils import _dpapi_unprotect
from collectors.browser import _open_browser_context, _apply_stealth
from sync_state import _sync_log, _app_log


def _app_log(msg):
    try:
        from main import _app_log
        _app_log(msg)
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
        _app_log(f"⚠️ Chrome master key load failed ({cookie_db}): {e}")

    # Chrome keeps a lock on the live DB — read from a copy.
    tmp = tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False).name
    try:
        shutil.copyfile(cookie_db, tmp)
        con = sqlite3.connect(tmp)
        rows = con.execute(
            'SELECT host_key, name, encrypted_value, value FROM cookies').fetchall()
        con.close()
    except Exception as e:
        _app_log(f"⚠️ Chrome cookie DB read failed ({cookie_db}): {e}")
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
        _app_log(f"ℹ️ {os.path.basename(os.path.dirname(os.path.dirname(cookie_db)))}: "
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

def _mac_keychain_pw(service):
    """Read a Keychain generic-password by SERVICE name. The account (-a) for these
    items is the app name ('Chrome'/'Chromium'), NOT the service — passing the wrong
    -a makes the lookup fail, which silently left us with only the 'peanuts' key and
    decrypted 0 cookies. Query by service only."""
    try:
        import subprocess
        r = subprocess.run(
            ['security', 'find-generic-password', '-s', service, '-w'],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().encode('utf-8')
    except Exception:
        pass
    return None


def _mac_chrome_key():
    """AES-128 key for Chrome cookies on macOS (first candidate)."""
    return _mac_chrome_candidate_keys()[0]


def _mac_chrome_candidate_keys():
    """All AES-128 keys to try on macOS. A profile's cookies may have been written
    by real Chrome (channel='chrome' → 'Chrome Safe Storage' key) OR by Playwright's
    bundled Chromium (→ 'Chromium Safe Storage' key), and the same seed profile can
    hold a mix. So derive a key from BOTH Keychain services, then 'peanuts' (Chromium
    with no Keychain entry). Trying the wrong key yields garbage that fails strict
    utf-8 validation downstream, so order doesn't matter — only coverage does."""
    from hashlib import pbkdf2_hmac
    keys = []
    for service in ('Chrome Safe Storage', 'Chromium Safe Storage'):
        pw = _mac_keychain_pw(service)
        if pw:
            keys.append(pbkdf2_hmac('sha1', pw, b'saltysalt', 1003, dklen=16))
    keys.append(pbkdf2_hmac('sha1', b'peanuts', b'saltysalt', 1003, dklen=16))
    return keys


def _decrypt_one_mac(ev, keys, AES):
    """Try every candidate key with STRICT utf-8 validation. Returns the cleanly
    decrypted value, or None if no key produces valid plaintext (= garbage)."""
    body = ev[3:]
    for k in keys:
        try:
            dec = AES.new(k, AES.MODE_CBC, IV=b' ' * 16).decrypt(body)
            if not dec:
                continue
            pad = dec[-1]
            if pad < 1 or pad > 16:          # invalid PKCS#7 padding → wrong key
                continue
            raw = dec[:-pad]
            # Newer Chrome (v130+) prepends a 32-byte SHA256 domain hash to the
            # plaintext. Strip it if the remainder decodes cleanly; otherwise use raw.
            for cand in ((raw[32:], raw) if len(raw) > 32 else (raw,)):
                try:
                    return cand.decode('utf-8')   # strict — raises on garbage
                except UnicodeDecodeError:
                    continue
        except Exception:
            continue
    return None


def _decrypt_chrome_cookie_db_mac(cookie_db):
    """Decrypt Chrome/Chromium cookie DB on macOS (v10/v11 = AES-128-CBC)."""
    import shutil, tempfile
    try:
        from Cryptodome.Cipher import AES
    except ImportError:
        return []
    if not cookie_db or not os.path.exists(cookie_db):
        return []
    keys = _mac_chrome_candidate_keys()
    tmp = tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False).name
    try:
        shutil.copyfile(cookie_db, tmp)
        con = sqlite3.connect(tmp)
        rows = con.execute(
            'SELECT host_key, name, encrypted_value, value FROM cookies').fetchall()
        con.close()
    except Exception as e:
        _app_log(f"⚠️ Mac Chrome cookie DB read failed ({cookie_db}): {e}")
        return []
    finally:
        try: os.remove(tmp)
        except: pass
    out, skipped = [], 0
    for host, name, ev, plain in rows:
        ev = bytes(ev) if ev else b''
        val = None
        if not ev:
            val = plain or ''
        elif ev[:3] in (b'v10', b'v11'):
            val = _decrypt_one_mac(ev, keys, AES)
        elif ev[:3] == b'v20':
            continue  # App-Bound Encrypted — undecryptable outside Chrome
        else:
            val = plain or ''
        if val is None:
            skipped += 1
            continue
        out.append({'name': name, 'value': val, 'domain': host.lstrip('.')})
    if skipped:
        _app_log(f"ℹ️ Mac Chrome cookies: {len(out)} decrypted, {skipped} undecryptable (wrong key/ABE) skipped — {cookie_db}")
    return out


def _load_appprofile_cookies_mac():
    """Read cookies from all app Chrome profiles on macOS."""
    import glob
    roots = {_BASE_DIR, os.getcwd()}
    profiles = set()
    for r in roots:
        profiles.update(glob.glob(os.path.join(r, 'chrome_profile*')))
        profiles.update(glob.glob(os.path.join(r, '*_profile')))
    seen, out = set(), []
    for prof in sorted(profiles):
        for sub in (os.path.join(prof, 'Default', 'Network', 'Cookies'),
                    os.path.join(prof, 'Default', 'Cookies')):
            if os.path.exists(sub):
                for c in _decrypt_chrome_cookie_db_mac(sub):
                    k = (c['domain'], c['name'])
                    if k not in seen:
                        seen.add(k)
                        out.append(c)
                break
    return out


def _latin1_safe_cookies(cookies):
    """HTTP headers are encoded latin-1, so a cookie whose value (or name) carries
    chars outside 0x00–0xFF cannot be sent and crashes requests with
    'latin-1 codec can't encode'. Cookies are ASCII by spec, so any such value is
    garbage (failed decryption / replacement chars) — drop it. This is the single
    choke point every collector reads through, so one filter protects all stocks."""
    clean, dropped = [], 0
    for c in cookies:
        name = c.get('name', '')
        val = c.get('value', '')
        try:
            name.encode('latin-1'); val.encode('latin-1')
        except (UnicodeEncodeError, AttributeError):
            dropped += 1
            continue
        clean.append(c)
    if dropped:
        _app_log(f"ℹ️ Dropped {dropped} non-latin-1 (corrupt) cookies before use")
    return clean


def _load_browser_cookies():
    """OS-agnostic: returns all browser cookies as list of dicts.
    On macOS: app Chrome profiles first (if browser-login was done), then Safari.
    On Windows: the app's own Chrome login profiles (variant 3).
    Always passes through _latin1_safe_cookies so corrupt values can't crash
    collectors (the 'latin-1 codec' bug that took down every stock)."""
    if IS_MAC:
        # 1) Playwright cookie cache — captured during the login window, decrypted by
        #    Playwright itself. Primary source on macOS (profile DBs aren't decryptable).
        cached = _load_pw_cookie_cache()
        if cached:
            return _latin1_safe_cookies(cached)
        # 2) App Chrome profiles (works only if the keychain key happens to match).
        #    NO Safari fallback: the architecture is "open the in-app isolated Chrome
        #    profile, log into each stock, close — the app reads cookies back from THAT
        #    profile" (via the pw cache above). Safari isn't part of the login flow and
        #    its container is sandbox-blocked anyway (Operation not permitted spam).
        chrome_cookies = _load_appprofile_cookies_mac()
        if chrome_cookies:
            return _latin1_safe_cookies(chrome_cookies)
        return []
    if IS_WIN:
        return _latin1_safe_cookies(_load_appprofile_cookies_windows())
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
        _app_log(f"[cookies] inject {stock_name} failed: {e}")
        return 0


# Domain patterns per stock for cookie filtering when importing from native Chrome
_STOCK_COOKIE_DOMAINS = {
    "Adobe Stock":   ["adobe.com", "stock.adobe.com", "contributor.stock.adobe.com",
                      "ims-na1.adobelogin.com"],
    "Shutterstock":  ["shutterstock.com", "submit.shutterstock.com"],
    "Getty Images":  ["gettyimages.com", "esp.gettyimages.com",
                      "accountmanagement.gettyimages.com"],
    "Depositphotos": ["depositphotos.com"],
    "Envato":        ["envato.com", "account.envato.com", "author.envato.com",
                      "elements.envato.com"],
    "Microstock+":   ["microstock.plus"],
    "Freepik":       ["magnific.com", "contributor.magnific.com", "freepik.com"],
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
    "Envato":        "https://author.envato.com/reports/performance",
    "Freepik":       "https://contributor.magnific.com/statistics",
}


# Auth-marker cookies whose presence (non-expired) proves a live session. ANY of
# the listed names counts. These are the names actually readable from the app's
# Chrome profiles when logged in (verified against real profiles) — NOT the
# HttpOnly server-only names. For stocks without a single obvious marker we fall
# back to "has any live cookie on the stock's domains" (Adobe/Depositphotos).
_STOCK_AUTH_COOKIE = {
    "Shutterstock": ["accts_contributor"],          # same marker the direct collector checks
    "Getty Images": ["ccw"],                         # ESP auth token
    # author.envato.com's _author_warehouse_session is HttpOnly and not readable
    # from the profile, so key off the account-session cookies that ARE present.
    "Envato":       ["envatosession", "envatoid", "_author_warehouse_session"],
    "Microstock+":  ["koa.sid"],
}


def _stock_has_valid_session(stock_name):
    """True if a live (non-expired) session for this stock exists in the app's
    Chrome login profiles. Used to open login windows ONLY for stocks that aren't
    already logged in. Reads the same cookies the collectors use, so it agrees with
    what a sync would actually see."""
    import time as _t
    domains = _STOCK_COOKIE_DOMAINS.get(stock_name, [])
    if not domains:
        return False
    try:
        cookies = _load_browser_cookies()
    except Exception:
        return False
    # GENERIC session detection (works for every stock, including future ones):
    # the in-app login profile is the source of truth. After you log into a tab,
    # the site leaves live cookies on its domains. We trust those. A named marker
    # (_STOCK_AUTH_COOKIE) is only a FAST positive — never a gate. The old code
    # REQUIRED a specific marker name for Getty/Envato/Shutterstock/MS+, but those
    # names are either HttpOnly or minted only on a page we never land on (Getty's
    # `ccw` lives on esp.* but login lands on accountmanagement.*), so freshly
    # logged-in stocks were reported "not logged in". Don't reintroduce that gate.
    markers = _STOCK_AUTH_COOKIE.get(stock_name) or []
    now = _t.time()
    domain_hits = 0
    for c in cookies:
        dom = (c.get('domain') or '').lstrip('.')
        if not any(d in dom for d in domains):
            continue
        exp = c.get('expires', -1)
        # expires -1/0 = session cookie (valid while present); >0 must be in future.
        if isinstance(exp, (int, float)) and exp > 0 and exp < now:
            continue
        if markers and c.get('name') in markers:
            return True          # fast positive
        domain_hits += 1
    # No marker (or marker is HttpOnly/on another subdomain) → any live cookie on
    # the stock's domains means the user logged in inside the app profile.
    return domain_hits > 0


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
            # Real system Chrome — do NOT inject stealth. On macOS that would poison
            # the very datadome token this login is meant to mint cleanly (the bug
            # that kept getting Shutterstock blocked). Mirrors Windows behaviour.
            _apply_stealth(vis, real_chrome=True)
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
            # Snapshot cookies to the cache while the window is open (and once more at
            # close) — Playwright returns them decrypted, the only reliable source on
            # macOS. Without this the direct collectors see "no browser cookies".
            _wait_close_capturing(vis, timeout_ms=600000)
            try:
                vis.close()
            except Exception:
                pass
    except Exception as e:
        _app_log(f"[win-login] failed: {e}")
        return [{'stock': s, 'imported': 0, 'error': str(e)} for s in opened or stocks]

    return [{'stock': s, 'imported': 1, 'msg': 'login window closed'} for s in opened]

