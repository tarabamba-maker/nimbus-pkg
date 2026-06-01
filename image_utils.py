"""
image_utils.py — Image download/cache helpers + MS library + group persistence.

Moved here from main.py (logic unchanged — only location changed):
  - _save_asset_meta
  - load_img, load_match_thumb, load_img_async
  - _img_executor, _http_executor
  - load_groups, save_groups
  - load_ms_library, save_ms_library
"""

import atexit
import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor as _TPE
from datetime import datetime
from io import BytesIO

import requests as req_lib
from PIL import Image, ImageOps

from db import DB_NAME
from utils import _dhash_from_path, _adobe_clean_thumb_url

_BASE_DIR = os.environ.get(
    "STOCK_DATA_DIR",
    os.path.dirname(os.path.abspath(__file__))
)
CACHE_DIR       = os.path.join(_BASE_DIR, "img_cache")
MATCH_CACHE_DIR = os.path.join(_BASE_DIR, "img_cache_match")
RECIPES_DIR     = os.path.join(_BASE_DIR, "recipes")
GROUPS_FILE     = os.path.join(RECIPES_DIR, "photo_groups.json")
MS_LIBRARY_FILE = os.path.join(RECIPES_DIR, "ms_library.json")

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(MATCH_CACHE_DIR, exist_ok=True)


def _save_asset_meta(stock, asset_id, path):
    """Compute hash + dominant RGB for thumbnail at path and persist to asset_meta."""
    h, ar, r, g, b = _dhash_from_path(path)
    if not h: return
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.execute("""INSERT INTO asset_meta (stock, asset_id, thumb_hash, aspect_ratio, r, g, b, updated_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                         ON CONFLICT(stock, asset_id) DO UPDATE SET
                           thumb_hash=excluded.thumb_hash, aspect_ratio=excluded.aspect_ratio,
                           r=excluded.r, g=excluded.g, b=excluded.b,
                           updated_at=excluded.updated_at""",
                      (stock or '', str(asset_id), h, ar, r, g, b, datetime.now().isoformat()))
            c.commit()
    except Exception:
        pass


def load_img(asset_id, url, stock=None):
    path = os.path.join(CACHE_DIR, f"{asset_id}.jpg")
    if os.path.exists(path): return path
    if not url: return None
    candidates = []
    if "stock.adobe.com" in url or "_F_" in url:
        candidates = [
            re.sub(r'\d{3}_F_', '500_F_', url),
            re.sub(r'\d{3}_F_', '360_F_', url),
        ]
    candidates.append(url)
    for u in candidates:
        if not u: continue
        try:
            r = req_lib.get(u, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                img = Image.open(BytesIO(r.content)).convert("RGB")
                img = ImageOps.fit(img, (400, 400), Image.Resampling.LANCZOS)
                img.save(path, "JPEG", quality=88)
                if stock:
                    _save_asset_meta(stock, asset_id, path)
                return path
        except Exception: continue
    return None


_img_executor  = _TPE(max_workers=14)
_http_executor = _TPE(max_workers=8)
atexit.register(lambda: (_img_executor.shutdown(wait=False), _http_executor.shutdown(wait=False)))


def load_match_thumb(asset_id, thumb_url):
    """Завантажує clean Adobe thumbnail (без watermark) у MATCH_CACHE_DIR.
    Після збереження рахує pHash+RGB з цього CHISTOGO фото — це авторитетний відбиток
    для крос-стокового матчингу (вотермарка хоч і слабко, але псує dHash gradient)."""
    path = os.path.join(MATCH_CACHE_DIR, f"{asset_id}.jpg")
    if os.path.exists(path):
        # Backfill: meta could be missing if path was created in older build
        _save_asset_meta("Adobe Stock", asset_id, path)
        return path
    clean_url = _adobe_clean_thumb_url(thumb_url)
    if not clean_url: return None
    try:
        r = req_lib.get(clean_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            img = Image.open(BytesIO(r.content)).convert("RGB")
            # 400x400 JPEG 88 — same form as load_img() and MS+ thumbs.
            # Consistent input → consistent pHash → reliable cross-matching.
            img = ImageOps.fit(img, (400, 400), Image.Resampling.LANCZOS)
            img.save(path, "JPEG", quality=88)
            # Adobe pHash MUST come from the clean version, not the watermarked img_cache one.
            _save_asset_meta("Adobe Stock", asset_id, path)
            return path
    except Exception:
        pass
    return None


def load_img_async(asset_id, url, callback, is_adobe=False, stock=None):
    def run():
        # For Adobe, skip pHash computation here — it'll be (re)computed from the
        # clean match thumbnail inside load_match_thumb below.
        meta_stock = None if is_adobe else stock
        path = load_img(asset_id, url, stock=meta_stock)
        if is_adobe and url:
            _img_executor.submit(lambda: load_match_thumb(asset_id, url))
        if path and callback: callback(path)
    try:
        _img_executor.submit(run)
    except Exception:
        pass


def load_groups() -> dict:
    try:
        with open(GROUPS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_groups(groups: dict):
    # Deduplicate: remove duplicate IDs within each group
    clean = {name: list(dict.fromkeys(str(a) for a in ids))
             for name, ids in groups.items() if ids}
    with open(GROUPS_FILE, "w") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)


_ms_library_cache: list = []
_ms_library_mtime: float = 0.0


def load_ms_library() -> list:
    global _ms_library_cache, _ms_library_mtime
    try:
        mtime = os.path.getmtime(MS_LIBRARY_FILE)
        if mtime == _ms_library_mtime and _ms_library_cache:
            return _ms_library_cache
        with open(MS_LIBRARY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _ms_library_cache = data if isinstance(data, list) else data.get("photos", [])
        _ms_library_mtime = mtime
        return _ms_library_cache
    except Exception:
        return []


def save_ms_library(photos: list):
    """⚠️ Dedup by basepath — UNIQUE per MS+ photo. The previous
    (directory, filename) key was buggy: my new MS+ direct stores 'group' not
    'directory', so dedup key became ('', filename) → all photos with same
    camera filename collapsed → lost 40% of records (15K→9K)."""
    global _ms_library_cache, _ms_library_mtime
    seen = set(); deduped = []
    for p in photos:
        # Prefer basepath (unique), fall back to legacy (directory, filename)
        bp = p.get("basepath", "").strip()
        if bp:
            k = ("bp", bp)
        else:
            k = ("legacy", p.get("directory",""), p.get("filename",""))
        if k not in seen:
            seen.add(k); deduped.append(p)
    with open(MS_LIBRARY_FILE, "w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False)
    _ms_library_cache = deduped
    _ms_library_mtime = os.path.getmtime(MS_LIBRARY_FILE)
