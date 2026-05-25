#!/usr/bin/env python3
"""
One-shot script: re-downloads thumbnails that are under 20KB (likely upscaled 110px Adobe images).
Tries 500_F_ first for Adobe CDN URLs. Saves at 400×400 JPEG 88 for Retina quality.
Run standalone — does NOT require the Flask app to be running.
"""
import os, sqlite3, re, sys
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageOps
import requests

DB   = os.path.join(os.path.dirname(__file__), 'sales.db')
CACHE = os.path.join(os.path.dirname(__file__), 'img_cache')
SIZE_THRESHOLD = 20 * 1024   # re-download files < 20 KB
WORKERS = 8
TARGET = (400, 400)

def candidates(url):
    if '_F_' in url:
        return [re.sub(r'\d{3}_F_', '500_F_', url),
                re.sub(r'\d{3}_F_', '360_F_', url),
                url]
    return [url]

def refresh(asset_id, url):
    path = os.path.join(CACHE, f'{asset_id}.jpg')
    for u in candidates(url):
        try:
            r = requests.get(u, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code == 200 and r.content[:2] == b'\xff\xd8':
                img = Image.open(BytesIO(r.content)).convert('RGB')
                img = ImageOps.fit(img, TARGET, Image.Resampling.LANCZOS)
                img.save(path, 'JPEG', quality=88)
                return True, asset_id
        except Exception:
            continue
    return False, asset_id

def main():
    # Collect small files
    small = set()
    for fname in os.listdir(CACHE):
        if not fname.endswith('.jpg'): continue
        fpath = os.path.join(CACHE, fname)
        if os.path.getsize(fpath) < SIZE_THRESHOLD:
            small.add(fname[:-4])   # strip .jpg → asset_id

    print(f'Found {len(small)} thumbnails under 20KB to refresh')

    # Get thumb URLs from DB
    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT asset_id, MAX(thumb_url) FROM sales "
            "WHERE thumb_url != '' AND thumb_url IS NOT NULL "
            "GROUP BY asset_id"
        ).fetchall()

    todo = [(aid, url) for aid, url in rows if aid in small and url]
    print(f'{len(todo)} have a thumb_url in DB → starting re-download with {WORKERS} workers')

    done = ok = fail = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(refresh, aid, url): aid for aid, url in todo}
        for fut in as_completed(futs):
            success, aid = fut.result()
            done += 1
            if success: ok += 1
            else: fail += 1
            if done % 200 == 0 or done == len(todo):
                pct = done / len(todo) * 100
                print(f'  {done}/{len(todo)} ({pct:.0f}%)  ok={ok}  fail={fail}', flush=True)

    print(f'\nDone. Refreshed {ok} thumbnails, {fail} failed.')

if __name__ == '__main__':
    main()
