"""
app_globals.py — Shared path constants and helper functions for Flask route blueprints.
No Flask/route imports here — safe to import from any route file without circular deps.
"""

import json
import os

_BASE_DIR = os.environ.get(
    "STOCK_DATA_DIR",
    os.path.dirname(os.path.abspath(__file__))
)

DB_NAME         = os.path.join(_BASE_DIR, "sales.db")
CACHE_DIR       = os.path.join(_BASE_DIR, "img_cache")
MATCH_CACHE_DIR = os.path.join(_BASE_DIR, "img_cache_match")
MS_CACHE_DIR    = os.path.join(_BASE_DIR, "img_cache_ms")
ICON_CACHE_DIR  = os.path.join(_BASE_DIR, "img_cache_icons")

RECIPES_DIR           = os.path.join(_BASE_DIR, "recipes")
MATCHES_FILE          = os.path.join(RECIPES_DIR, "_cross_stock_matches.json")
MANUAL_OVERRIDES_FILE = os.path.join(RECIPES_DIR, "_manual_overrides.json")
GROUPS_FILE           = os.path.join(RECIPES_DIR, "photo_groups.json")
MS_LIBRARY_FILE       = os.path.join(RECIPES_DIR, "ms_library.json")
PROCESSED_DATES_FILE  = os.path.join(RECIPES_DIR, "_processed_dates.json")

STOCKS = [
    "Adobe Stock", "Shutterstock", "Getty Images", "Depositphotos",
]

_STOCK_KEY = {
    "Adobe Stock": "adobe", "Shutterstock": "shutterstock",
    "iStock": "istock", "iStockphoto": "istock",
    "Getty Images": "getty", "Depositphotos": "depositphotos",
    "Pond5": "pond5", "Alamy": "alamy",
}

_RELEVANT_STOCKS = {'adobestock', 'shutterstock', 'istock', 'esp', 'depositphotos'}


def _load_matches() -> dict:
    try:
        with open(MATCHES_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_matches(m: dict):
    with open(MATCHES_FILE, "w") as f:
        json.dump(m, f, indent=2)


def _load_overrides() -> dict:
    try:
        with open(MANUAL_OVERRIDES_FILE) as f:
            d = json.load(f)
            return {"linked": d.get("linked", {}), "unlinked": d.get("unlinked", {})}
    except Exception:
        return {"linked": {}, "unlinked": {}}


def _save_overrides(d: dict):
    with open(MANUAL_OVERRIDES_FILE, "w") as f:
        json.dump({"linked": d.get("linked", {}), "unlinked": d.get("unlinked", {})}, f, indent=2)
