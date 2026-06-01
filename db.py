"""
db.py — SQLite helpers with NO dependencies on Flask, sync state, or Playwright.

Functions moved here from main.py (logic unchanged — only location changed):
  - init_db
  - is_already_saved
  - save_to_db

DB_NAME is resolved from STOCK_DATA_DIR env var (same logic as main.py constant).
Both files will always resolve to the same path at runtime.
"""

import os
import sqlite3
from datetime import datetime

# Same resolution logic as _BASE_DIR / DB_NAME in main.py.
_BASE_DIR = os.environ.get("STOCK_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_NAME   = os.path.join(_BASE_DIR, "sales.db")


def init_db():
    with sqlite3.connect(DB_NAME, timeout=15) as c:
        # Concurrency + speed PRAGMAs. WAL lets readers run in parallel with
        # writers; synchronous=NORMAL is safe with WAL and 2-5× faster than
        # FULL on heavy insert sessions. journal_mode is persistent on the DB
        # file, but cheap to set every startup.
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA temp_store=MEMORY")
            c.execute("PRAGMA mmap_size=268435456")
        except Exception: pass
        c.execute('''CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT, photo_name TEXT,
            stock TEXT DEFAULT "Adobe Stock",
            price REAL, thumb_url TEXT,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            filename TEXT)''')
        try: c.execute("ALTER TABLE sales ADD COLUMN filename TEXT")
        except Exception: pass
        # Covers is_already_saved() (stock+asset_id+date prefix) and feed/stats
        # range scans by date. Without this each dedup check is a full table scan.
        c.execute("CREATE INDEX IF NOT EXISTS idx_sales_dedup ON sales(stock, asset_id, date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sales_date  ON sales(date)")

        # Per-asset perceptual hash + dominant RGB + aspect_ratio for visual matching.
        c.execute('''CREATE TABLE IF NOT EXISTS asset_meta (
            stock TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            thumb_hash TEXT,
            aspect_ratio REAL,
            r INTEGER, g INTEGER, b INTEGER,
            updated_at TEXT,
            PRIMARY KEY (stock, asset_id))''')
        for col, sqltype in [("r", "INTEGER"), ("g", "INTEGER"), ("b", "INTEGER")]:
            try: c.execute(f"ALTER TABLE asset_meta ADD COLUMN {col} {sqltype}")
            except Exception: pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_asset_meta_hash ON asset_meta(thumb_hash)")

        # MS+ reference thumbnails (img_cache_ms/*.jpg) — used as ground-truth base
        # for visual matching. fname = disk filename WITHOUT .jpg extension.
        c.execute('''CREATE TABLE IF NOT EXISTS ms_meta (
            fname TEXT PRIMARY KEY,
            thumb_hash TEXT,
            aspect_ratio REAL,
            r INTEGER, g INTEGER, b INTEGER,
            updated_at TEXT)''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_ms_meta_hash ON ms_meta(thumb_hash)")

        # Таблиця фотографій — єдине джерело правди
        c.execute('''CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY,
            stock_ids TEXT DEFAULT "{}",
            thumb_url TEXT DEFAULT "",
            ms_folder TEXT DEFAULT "",
            groups TEXT DEFAULT "[]",
            earnings TEXT DEFAULT "{}",
            updated_at TEXT DEFAULT "")''')
        # Індекс (stock_key, asset_id) → photo_id для швидкого lookup
        c.execute('''CREATE TABLE IF NOT EXISTS photo_assets (
            stock_key TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            photo_id TEXT NOT NULL,
            PRIMARY KEY (stock_key, asset_id))''')
        c.commit()


def is_already_saved(stock, asset_id, price, date_str):
    """Returns True if this exact sale already exists in the DB.

    Two-pass strategy to handle both old (date-only) and new (datetime) records:
    1. If date_str has a time component (ISO from Adobe): exact datetime match
       against records stored with time.  Different sales of the same photo on
       the same day have different timestamps → correctly allowed.
    2. Fallback: price±0.005 + day match via substr(date,1,10). Matches BOTH
       legacy 10-char and current 19-char rows.

    ⚠️ DO NOT add `AND LENGTH(date)=10` — past bug: with all current rows in
    19-char format, that filter matched nothing → SS daily aggregates were
    re-inserted as duplicates every sync (6,241 dups deleted 2026-05-19).
    """
    if not asset_id:
        return False
    try:
        raw = str(date_str)
        day = raw[:10]
        for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                day = datetime.strptime(raw[:10], fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                pass
        p = float(price) if price else 0.0
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            # Pass 1: exact datetime (new records stored with HH:MM:SS)
            if 'T' in raw or (' ' in raw and len(raw) > 10):
                dt_norm = raw.replace('T', ' ').split('+')[0].split('Z')[0][:19]
                row = c.execute(
                    'SELECT 1 FROM sales WHERE stock=? AND asset_id=? AND date=?',
                    (stock, str(asset_id), dt_norm)
                ).fetchone()
                if row:
                    return True
            # Pass 2: price + day fallback — matches records stored in ANY
            # date format ("2026-04-29" or "2026-04-29 00:00:00") since SS/
            # Deposit collectors pass date-only strings while existing rows
            # are mixed-format. Old LENGTH(date)=10 restriction caused all
            # SS daily aggregates to be re-inserted as duplicates every sync.
            row = c.execute(
                'SELECT 1 FROM sales WHERE stock=? AND asset_id=? '
                'AND substr(date,1,10)=? AND ABS(price - ?) < 0.005',
                (stock, str(asset_id), day, p)
            ).fetchone()
        return row is not None
    except Exception:
        return False


def save_to_db(d):
    try:
        stock = d.get('stock', 'Adobe Stock')
        aid   = str(d.get('asset_id', ''))
        price = float(d.get('price') or 0)
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.execute(
                'INSERT INTO sales (asset_id,photo_name,stock,price,thumb_url,date,filename) '
                'VALUES (?,?,?,?,?,?,?)',
                (aid, d.get('photo_name'), stock, price,
                 d.get('thumb_url'), d.get('date'), d.get('filename')))
            c.commit()
    except Exception as e:
        print(f"[DB] {e}")
