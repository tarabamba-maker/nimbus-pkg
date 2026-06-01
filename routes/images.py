"""
routes/images.py — Image serving endpoints.

Moved verbatim from main.py. No logic changes.
"""

import base64
import os
import re
import sqlite3
from io import BytesIO

import requests as req_lib
from flask import Blueprint, Response, abort, make_response, send_file
from PIL import Image, ImageOps

from app_globals import CACHE_DIR, DB_NAME, ICON_CACHE_DIR, MS_CACHE_DIR
from sync_state import _app_log

images_bp = Blueprint('images', __name__)

_STOCK_FAVICON_URLS = {
    'adobe stock':   'https://stock.adobe.com/favicon.ico',
    'shutterstock':  'https://www.shutterstock.com/favicon.ico',
    'getty images':  'https://www.google.com/s2/favicons?domain=gettyimages.com&sz=64',
    'istock':        'https://www.google.com/s2/favicons?domain=istockphoto.com&sz=64',
    'istockphoto':   'https://www.google.com/s2/favicons?domain=istockphoto.com&sz=64',
    'depositphotos': 'https://depositphotos.com/favicon.ico',
}


@images_bp.route('/img/cache/<aid>')
def img_cache_serve(aid):
    """Serve img_cache/aid.jpg, or try to fetch and cache on-demand if missing."""
    p = os.path.join(CACHE_DIR, f"{aid}.jpg")
    if os.path.exists(p):
        return send_file(p, mimetype='image/jpeg')
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as _c:
            row = _c.execute("SELECT thumb_url FROM sales WHERE asset_id=? AND thumb_url!='' LIMIT 1", (aid,)).fetchone()
        if row and row[0]:
            raw_url = row[0]
            if '_F_' in raw_url:
                raw_url = re.sub(r'\d{3}_F_', '500_F_', raw_url)
            r = req_lib.get(raw_url, timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200 and raw_url != row[0]:
                r = req_lib.get(row[0], timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code == 200 and r.content[:2] == b'\xff\xd8':
                img = Image.open(BytesIO(r.content)).convert('RGB')
                img = ImageOps.fit(img, (400, 400), Image.Resampling.LANCZOS)
                img.save(p, 'JPEG', quality=88)
                return send_file(p, mimetype='image/jpeg')
    except Exception:
        pass
    abort(404)


@images_bp.route('/img/placeholder')
def img_placeholder():
    """Tiny gray JPEG placeholder for missing thumbnails."""
    b64 = (
        '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkS'
        'Ew8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJ'
        'CQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy'
        'MjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf/'
        'EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/'
        'aAAwDAQACEQMRAD8AJQAB/9k='
    )
    data = base64.b64decode(b64)
    return Response(data, mimetype='image/jpeg',
                    headers={'Cache-Control': 'public, max-age=86400'})


@images_bp.route('/img/ms/<fname>')
def img_ms_serve(fname):
    """Serve img_cache_ms/fname.jpg для Svelte UI."""
    p = os.path.join(MS_CACHE_DIR, f"{fname}.jpg")
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype='image/jpeg')


@images_bp.route('/img/stock-icon/<name>')
def img_stock_icon(name):
    """Fetch, cache and serve stock site favicon as 32×32 PNG."""
    safe = re.sub(r'[^a-z0-9]', '_', name.lower())
    p = os.path.join(ICON_CACHE_DIR, f"{safe}.png")
    if not os.path.exists(p):
        url = _STOCK_FAVICON_URLS.get(name.lower())
        if not url:
            abort(404)
        try:
            r = req_lib.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                abort(404)
            img = Image.open(BytesIO(r.content)).convert('RGBA')
            img = img.resize((32, 32), Image.Resampling.LANCZOS)
            img.save(p, 'PNG')
        except Exception as e:
            _app_log(f"[stock-icon] failed {url}: {e}")
            abort(404)
    resp = make_response(send_file(p, mimetype='image/png'))
    resp.headers['Cache-Control'] = 'public, max-age=604800'
    return resp
