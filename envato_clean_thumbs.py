"""
envato_clean_thumbs.py — replace watermarked Envato thumbnails with CLEAN ones
from the portfolio (portfolio.uuid == our asset_id).

For every sold Envato photo:
  1. delete the old watermarked img_cache/<id>.jpg + its asset_meta row
  2. load_img(id, portfolio_thumbnail_url) → ImageOps.fit 400x400 center-square
     (same pipeline as MS+ thumbs) → recomputes clean asset_meta dHash too.

Result: clean display thumbs (100% by ID) AND clean pHash hashes that match MS+
(median hamming ~0 for true matches) → ready for _ms_visual_matches auto-grouping.
"""
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

SUP = os.path.expanduser("~/Library/Application Support/StockAutomation")
os.environ.setdefault("STOCK_DATA_DIR", SUP)

import json
from image_utils import load_img, CACHE_DIR
from db import DB_NAME

port = json.load(open(os.path.join(SUP, "recipes", "_envato_portfolio.json")))

with sqlite3.connect(DB_NAME, timeout=30) as c:
    ids = [r[0] for r in c.execute(
        "SELECT DISTINCT asset_id FROM sales WHERE stock='Envato' "
        "AND asset_id NOT LIKE 'envato-%'")]
print(f"Envato sold ids: {len(ids)} | portfolio map: {len(port)}")

# 1. purge old watermarked thumbs + asset_meta rows
purged = 0
for aid in ids:
    p = os.path.join(CACHE_DIR, f"{aid}.jpg")
    if os.path.exists(p):
        os.remove(p); purged += 1
with sqlite3.connect(DB_NAME, timeout=30) as c:
    c.execute("DELETE FROM asset_meta WHERE stock='Envato'")
print(f"purged {purged} old thumbs + asset_meta envato rows")

# 2. download clean thumbs (parallel)
todo = [(aid, port[aid]) for aid in ids if port.get(aid)]
missing = [aid for aid in ids if not port.get(aid)]
print(f"to download: {len(todo)} | no portfolio url: {len(missing)}")

ok = fail = 0
def _one(aid, url):
    try:
        return aid, bool(load_img(aid, url, stock="Envato"))
    except Exception:
        return aid, False

with ThreadPoolExecutor(max_workers=16) as pool:
    futs = [pool.submit(_one, aid, url) for aid, url in todo]
    for i, f in enumerate(as_completed(futs), 1):
        aid, good = f.result()
        ok += good; fail += (not good)
        if i % 500 == 0:
            print(f"  {i}/{len(todo)}  ok={ok} fail={fail}")

print(f"\n✅ done: clean thumbs ok={ok} fail={fail}")
with sqlite3.connect(DB_NAME, timeout=30) as c:
    am = c.execute("SELECT COUNT(*) FROM asset_meta WHERE stock='Envato'").fetchone()[0]
print(f"asset_meta Envato rows (clean hashes): {am}")
