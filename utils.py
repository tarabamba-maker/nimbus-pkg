"""
utils.py — Pure utility functions with NO dependencies on Flask, SQLite,
sync state, or any other app module. Safe to import from anywhere.

Functions moved here from main.py (logic unchanged — only location changed):
  - _adobe_clean_thumb_url
  - _hamming_hex
  - _dhash_from_path
  - _parse_safari_binarycookies
  - _dpapi_unprotect
"""

import os
import re


# ── Adobe thumbnail URL cleanup ───────────────────────────────────────────────

def _adobe_clean_thumb_url(thumb_url: str) -> str:
    """
    Convert any Adobe ftcdn.net URL to a clean 110px version without watermark.
    as2.ftcdn.net/jpg/.../110_F_{id}_{hash}.jpg
    """
    if not thumb_url or "ftcdn.net" not in thumb_url:
        return thumb_url
    url = re.sub(r'/\d+_F_', '/110_F_', thumb_url)
    url = re.sub(r'https?://[^/]+\.ftcdn\.net', 'https://as2.ftcdn.net', url)
    return url


# ── Perceptual hashing ────────────────────────────────────────────────────────

def _hamming_hex(h1, h2):
    """Hamming distance between two 16-char hex strings. 0 = identical, 64 = totally different."""
    try:
        return bin(int(h1, 16) ^ int(h2, 16)).count('1')
    except Exception:
        return 64


def _dhash_from_path(path):
    """
    Compute 64-bit dHash + dominant RGB from an image file.
    Returns (hex_string, aspect_ratio, r, g, b) or (None, None, None, None, None).
    """
    try:
        from PIL import Image
        img = Image.open(path)
        ar = round(img.width / img.height, 3) if img.height else 1.0
        rgb_small = img.convert('RGB').resize((9, 8), Image.Resampling.LANCZOS)
        gray_px = list(rgb_small.convert('L').getdata())
        rgb_px  = list(rgb_small.getdata())
        bits = 0
        for row in range(8):
            base = row * 9
            for col in range(8):
                bits = (bits << 1) | (1 if gray_px[base + col] > gray_px[base + col + 1] else 0)
        n = len(rgb_px)
        r_avg = sum(p[0] for p in rgb_px) // n
        g_avg = sum(p[1] for p in rgb_px) // n
        b_avg = sum(p[2] for p in rgb_px) // n
        return f"{bits:016x}", ar, r_avg, g_avg, b_avg
    except Exception:
        return None, None, None, None, None


# ── Safari binarycookies parser ───────────────────────────────────────────────

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

    cookies = []
    cur = off
    for page_size in page_sizes:
        page = data[cur:cur+page_size]
        cur += page_size
        if len(page) < 4 or page[:4] != b'\x00\x00\x01\x00':
            continue
        num_cookies = struct.unpack('<I', page[4:8])[0]
        cookie_offsets = [struct.unpack('<I', page[8+i*4:12+i*4])[0] for i in range(num_cookies)]
        for co in cookie_offsets:
            try:
                size   = struct.unpack('<I', page[co:co+4])[0]
                flags  = struct.unpack('<I', page[co+8:co+12])[0]
                domain_off  = struct.unpack('<I', page[co+16:co+20])[0]
                name_off    = struct.unpack('<I', page[co+20:co+24])[0]
                path_off    = struct.unpack('<I', page[co+24:co+28])[0]
                value_off   = struct.unpack('<I', page[co+28:co+32])[0]
                base = co
                def _str(off):
                    end = page.index(b'\x00', base + off)
                    return page[base + off:end].decode('utf-8', 'replace')
                cookies.append({
                    'name':   _str(name_off),
                    'value':  _str(value_off),
                    'domain': _str(domain_off),
                    'path':   _str(path_off),
                    'secure': bool(flags & 1),
                })
            except Exception:
                continue
    return cookies


# ── Windows DPAPI ─────────────────────────────────────────────────────────────

def _dpapi_unprotect(data: bytes) -> bytes:
    """Unwrap a DPAPI-protected blob in the current user context (ctypes, no
    pywin32 dependency). Windows-only."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError('CryptUnprotectData failed')
    try:
        n = blob_out.cbData
        out = ctypes.create_string_buffer(n)
        ctypes.memmove(out, blob_out.pbData, n)
        return out.raw
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
