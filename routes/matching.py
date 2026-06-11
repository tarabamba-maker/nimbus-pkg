"""
routes/matching.py — Matching, hashing and cross-stock match endpoints.

Moved verbatim from main.py. No logic changes.
Routes: /api/refresh-istock-thumbs, /api/compute-hashes, /api/compute-ms-hashes,
        /api/rebuild-matches, /api/match-override, /api/matches
"""

import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.parse as _up
from datetime import datetime

import requests as req_lib
from flask import Blueprint, jsonify, request

from app_globals import (
    CACHE_DIR, DB_NAME, MS_CACHE_DIR, RECIPES_DIR,
    _RELEVANT_STOCKS,
    _load_matches, _save_matches, _load_overrides, _save_overrides,
)
from cookies import _load_browser_cookies
from image_utils import load_groups, save_groups, load_ms_library, load_img
from matching_engine import (
    _hash_based_matches, _filename_fallback_matches,
    _ms_visual_matches, _apply_manual_overrides,
)
from sync_state import _sync_state, _sync_stop_flag, _sync_log
from utils import _dhash_from_path

matching_bp = Blueprint('matching', __name__)

_PRIMARY_PRIORITY = ['adobestock', 'shutterstock', 'istock', 'esp', 'depositphotos']


def _pick_primary(stockids):
    for key in _PRIMARY_PRIORITY:
        v = stockids.get(key)
        if v: return str(v)
    return None


@matching_bp.route('/api/refresh-istock-thumbs', methods=['POST'])
def api_refresh_istock_thumbs():
    """Re-fetch ThumbnailUrl from ESP API for iStock asset_ids missing pHash."""
    with sqlite3.connect(DB_NAME, timeout=15) as c:
        all_istock = set(r[0] for r in c.execute(
            "SELECT DISTINCT asset_id FROM sales "
            "WHERE stock IN ('iStock','iStockphoto') AND asset_id IS NOT NULL"
        ) if r[0])
        hashed = set(r[0] for r in c.execute(
            "SELECT asset_id FROM asset_meta WHERE stock='iStock' "
            "AND thumb_hash IS NOT NULL AND thumb_hash != ''"
        ) if r[0])
    target = all_istock - hashed
    if not target:
        return jsonify({'status': 'ok', 'msg': 'no iStock aids missing pHash', 'updated': 0})

    try:
        all_cookies = _load_browser_cookies()
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'cookie read failed: {e}'}), 500
    if not all_cookies:
        return jsonify({'status': 'error',
                        'msg': 'no browser cookies (Safari on mac / Chrome on win)'}), 400

    g_cookies = {c['name']: c['value'] for c in all_cookies
                 if 'gettyimages' in c.get('domain', '').lower()}
    if 'ccw' not in g_cookies:
        return jsonify({'status': 'error', 'msg': 'no ccw cookie — login to Getty in Safari'}), 401

    try:
        ccw_raw = g_cookies['ccw']
        b64 = _up.unquote(ccw_raw).split("|")[0]
        b64 += "=" * (4 - len(b64) % 4)
        sts_token = json.loads(base64.b64decode(b64))["sts_token"]
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'sts_token decode: {e}'}), 500

    def _run():
        _sync_state["running"] = True
        _sync_state["log"] = []
        _sync_stop_flag[0] = False
        _sync_log(f"🚀 iStock thumb refresh: {len(target)} aids потребують pHash")

        esp_base = "https://esp.gettyimages.com"
        esp_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {sts_token}",
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                           "Version/17.5 Safari/605.1.15"),
        }
        import calendar as _cal
        from datetime import datetime as _dt
        now = _dt.now()
        remaining = set(target)
        updated = 0
        try:
            for offset_m in range(60):
                if _sync_stop_flag[0] or not remaining:
                    break
                m_i = now.month - offset_m
                y_i = now.year
                while m_i <= 0:
                    m_i += 12; y_i -= 1
                from_date = f"{y_i}-{m_i:02d}-01"
                last_d = _cal.monthrange(y_i, m_i)[1]
                to_date = f"{y_i}-{m_i:02d}-{last_d:02d}"
                if y_i == now.year and m_i == now.month:
                    to_date = now.strftime("%Y-%m-%d")
                page_n = 1
                while True:
                    if _sync_stop_flag[0] or not remaining: break
                    url = (f"{esp_base}/api/account/v1/statistics/downloads_for_search"
                           f"?orderResultsBy=LastDownloadDate&sortDirection=Descending"
                           f"&page={page_n}&pageSize=50"
                           f"&fromDate={from_date}&toDate={to_date}"
                           f"&primaryDatePeriod=by_month")
                    try:
                        r = req_lib.get(url, headers=esp_headers, cookies=g_cookies, timeout=20)
                        if r.status_code != 200:
                            _sync_log(f"  ⚠️ ESP {y_i}-{m_i:02d} p{page_n}: HTTP {r.status_code}")
                            break
                        data = r.json()
                    except Exception as ex:
                        _sync_log(f"  ⚠️ ESP {y_i}-{m_i:02d} p{page_n}: {ex}")
                        break
                    items = data.get("AssetDownloadSummaries", []) or []
                    if not items: break
                    for item in items:
                        aid = str(item.get("MasterId", ""))
                        thumb = item.get("ThumbnailUrl", "")
                        if not aid or not thumb or aid not in remaining:
                            continue
                        cached = os.path.join(CACHE_DIR, f"{aid}.jpg")
                        if os.path.exists(cached):
                            try: os.remove(cached)
                            except Exception: pass
                        path = load_img(aid, thumb, stock="iStock")
                        if path:
                            updated += 1
                            remaining.discard(aid)
                    total_pages = (data.get("TotalAssetCount", 0) + 49) // 50
                    if page_n >= total_pages: break
                    page_n += 1
                    time.sleep(0.1)
                _sync_log(f"  📦 {y_i}-{m_i:02d}: оновлено {updated}, лишилось {len(remaining)}")
            _sync_log(f"✅ iStock thumbs: оновлено {updated} з {len(target)} (лишилось {len(remaining)})")

            try:
                resp = api_rebuild_matches()
                _sync_log(f"✅ rebuild-matches: {resp.get_json()}")
            except Exception as ex:
                _sync_log(f"⚠️ rebuild-matches: {ex}")
        finally:
            _sync_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'ok', 'msg': f'iStock refresh started for {len(target)} aids',
                    'targeted': len(target)})


@matching_bp.route('/api/compute-hashes', methods=['POST'])
def api_compute_hashes():
    """Backfill thumb_hash + dominant RGB for every cached thumbnail in img_cache/."""
    if not os.path.isdir(CACHE_DIR):
        return jsonify({'computed': 0, 'msg': 'no cache dir'})

    aid_stock = {}
    with sqlite3.connect(DB_NAME, timeout=15) as c:
        for aid, stock in c.execute("SELECT DISTINCT asset_id, stock FROM sales WHERE asset_id IS NOT NULL"):
            if aid and stock and aid not in aid_stock:
                aid_stock[str(aid)] = stock
        complete = set(
            (s, a) for s, a in c.execute(
                "SELECT stock, asset_id FROM asset_meta WHERE r IS NOT NULL"
            )
        )

    computed = 0
    skipped = 0
    rows = []
    for fname in os.listdir(CACHE_DIR):
        if not fname.lower().endswith('.jpg'):
            continue
        aid = fname[:-4]
        stock = aid_stock.get(aid)
        if not stock:
            skipped += 1
            continue
        if (stock, aid) in complete:
            continue
        h, ar, r, g, b = _dhash_from_path(os.path.join(CACHE_DIR, fname))
        if not h:
            continue
        rows.append((stock, aid, h, ar, r, g, b, datetime.now().isoformat()))
        computed += 1

    if rows:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.executemany(
                "INSERT OR REPLACE INTO asset_meta "
                "(stock, asset_id, thumb_hash, aspect_ratio, r, g, b, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows
            )
            c.commit()
    return jsonify({'computed': computed, 'skipped_no_stock': skipped,
                    'total_complete': len(complete) + computed})


@matching_bp.route('/api/compute-ms-hashes', methods=['POST'])
def api_compute_ms_hashes():
    """Backfill thumb_hash + dominant RGB for every reference thumbnail in img_cache_ms/."""
    if not os.path.isdir(MS_CACHE_DIR):
        return jsonify({'computed': 0, 'msg': 'no ms cache dir'})

    with sqlite3.connect(DB_NAME, timeout=15) as c:
        complete = set(r[0] for r in c.execute("SELECT fname FROM ms_meta WHERE r IS NOT NULL"))

    computed = 0
    rows = []
    for fname in os.listdir(MS_CACHE_DIR):
        if not fname.lower().endswith('.jpg'):
            continue
        base = fname[:-4]
        if base in complete:
            continue
        h, ar, r, g, b = _dhash_from_path(os.path.join(MS_CACHE_DIR, fname))
        if not h:
            continue
        rows.append((base, h, ar, r, g, b, datetime.now().isoformat()))
        computed += 1
        if len(rows) >= 1000:
            with sqlite3.connect(DB_NAME, timeout=15) as c:
                c.executemany(
                    "INSERT OR REPLACE INTO ms_meta "
                    "(fname, thumb_hash, aspect_ratio, r, g, b, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)", rows)
                c.commit()
            rows = []

    if rows:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            c.executemany(
                "INSERT OR REPLACE INTO ms_meta "
                "(fname, thumb_hash, aspect_ratio, r, g, b, updated_at) "
                "VALUES (?,?,?,?,?,?,?)", rows)
            c.commit()
    return jsonify({'computed': computed,
                    'total_complete': len(complete) + computed})


# Serializes rebuild-matches: callers are the UI button (Flask request thread),
# the post-sync auto-rebuild (orchestrator background thread) and the iStock
# thumbs refresh thread. Concurrent runs would read-modify-write the same JSONs
# (_cross_stock_matches / photo_groups / _matches_state) and lose data.
_rebuild_lock = threading.Lock()


@matching_bp.route('/api/rebuild-matches', methods=['POST'])
def api_rebuild_matches(force=False):
    """Rebuild cross-stock matches + sync ms_library groups into photo_groups.json.
    POST {"force": true} wipes the incremental state + group snapshot so the whole
    catalog is re-clustered from scratch (the normal call early-exits 'cached' when
    nothing changed). Internal callers (rebuild-from-db) pass force=True directly."""
    with _rebuild_lock:
        return _rebuild_matches_locked(force)


def _rebuild_matches_locked(force=False):
    lib      = load_ms_library()
    overrides = _load_overrides()

    # ── State + change detection ─────────────────────────────────────────
    _state_file    = os.path.join(RECIPES_DIR, '_matches_state.json')
    _snapshot_file = os.path.join(RECIPES_DIR, '_ms_group_snapshot.json')
    # Auto-rebuild after sync calls this from a background thread (NO request
    # context) — guard the request access or it raises "Working outside of request
    # context" and the rebuild aborts (groups vanish).
    from flask import has_request_context
    _force = bool(force)
    if not _force and has_request_context():
        _force = bool((request.get_json(force=True, silent=True) or {}).get('force'))
    if _force:
        for _f in (_state_file, _snapshot_file,
                   os.path.join(RECIPES_DIR, '_cross_stock_matches.json')):
            try:
                if os.path.exists(_f):
                    os.remove(_f)
            except Exception:
                pass
    try:
        with open(_state_file) as _f: _state = json.load(_f)
    except Exception:
        _state = {}
    _last_am_rowid = _state.get('last_asset_meta_rowid')
    _last_mm_rowid = _state.get('last_ms_meta_rowid')
    _last_lib_fp   = _state.get('last_ms_lib_fingerprint')

    with sqlite3.connect(DB_NAME, timeout=15) as _c:
        _r = _c.execute("SELECT MAX(rowid) FROM asset_meta WHERE thumb_hash IS NOT NULL AND thumb_hash != ''").fetchone()
        _cur_am_rowid = _r[0] or 0
        _r = _c.execute("SELECT MAX(rowid) FROM ms_meta WHERE thumb_hash IS NOT NULL AND thumb_hash != ''").fetchone()
        _cur_mm_rowid = _r[0] or 0

    _h = hashlib.sha1()
    for p in sorted(lib, key=lambda x: x.get('basepath', '')):
        _h.update(repr((p.get('basepath', ''), p.get('group', ''),
                        tuple(sorted((p.get('stockids') or {}).items())))).encode())
    _cur_lib_fp = _h.hexdigest()

    if _last_am_rowid is not None and _last_am_rowid > _cur_am_rowid:
        _last_am_rowid = None
    if _last_mm_rowid is not None and _last_mm_rowid > _cur_mm_rowid:
        _last_mm_rowid = None

    am_unchanged  = (_last_am_rowid == _cur_am_rowid)
    mm_unchanged  = (_last_mm_rowid == _cur_mm_rowid)
    lib_unchanged = (_last_lib_fp == _cur_lib_fp)

    # ── Early exit ───────────────────────────────────────────────────────
    matches_path = os.path.join(RECIPES_DIR, '_cross_stock_matches.json')
    if (am_unchanged and mm_unchanged and lib_unchanged
            and _last_am_rowid is not None and os.path.exists(matches_path)):
        cur_matches = _load_matches()
        cur_groups  = load_groups()
        return jsonify({'status': 'ok', 'cached': True,
                        'entries': len(cur_matches),
                        'photos': 0, 'groups': len(cur_groups),
                        'hash_pairs': 0, 'ms_lib_pairs': 0,
                        'filename_pairs': 0, 'auto_grouped': 0,
                        'ms_visual_grouped': 0, 'override_changes': 0})

    # ── Pass A: pHash+RGB clustering (incremental) ───────────────────────
    if _last_am_rowid is None:
        matches, hash_pairs, _new_am_rowid = _hash_based_matches({}, incremental_from_rowid=None)
    else:
        matches, hash_pairs, _new_am_rowid = _hash_based_matches(
            _load_matches(), incremental_from_rowid=_last_am_rowid)

    # Pass B (ms_library stockids clustering) removed — matching is pHash+RGB only.
    # Pass C (filename fallback) removed — always returned 0 pairs; Pass A/F cover this.
    ms_added_pairs = 0
    fn_pairs = 0
    _save_matches(matches)

    # ── Pass D: sync MS+ groups → photo_groups (DIFF-BASED) ──────────────
    # On force, start groups from SCRATCH so old bloat (wrong-shoot photos that
    # aren't ms_authoritative primaries) is fully discarded — the incremental purge
    # below only removes ms_library primaries+siblings, not unrelated garbage.
    groups = {} if _force else load_groups()
    ms_authoritative = {}
    for photo in lib:
        gname = (photo.get('group') or '').strip()
        if not gname: continue
        stockids = photo.get('stockids') or {}
        primary = _pick_primary(stockids)
        if not primary: continue
        ms_authoritative[primary] = gname

    try:
        with open(_snapshot_file) as _f: _snapshot = json.load(_f)
    except Exception:
        _snapshot = {}

    aid_to_key_purge = {m: k for k, members in matches.items() for m in members}
    photos_synced = 0

    if not _snapshot:
        purge_aids = set()
        for primary in ms_authoritative:
            purge_aids.add(primary)
            ck = aid_to_key_purge.get(primary)
            if ck:
                purge_aids.update(matches.get(ck, []))
        if purge_aids:
            for gname in list(groups.keys()):
                groups[gname] = [a for a in groups[gname] if a not in purge_aids]
        for primary, gname in ms_authoritative.items():
            if gname not in groups: groups[gname] = []
            if primary not in groups[gname]:
                groups[gname].append(primary)
                photos_synced += 1
    else:
        diff_added = {p: g for p, g in ms_authoritative.items() if p not in _snapshot}
        diff_moved = {p: g for p, g in ms_authoritative.items()
                      if p in _snapshot and _snapshot[p] != g}

        for primary, new_gname in diff_moved.items():
            old_gname = _snapshot.get(primary, '')
            purge_set = {primary}
            ck = aid_to_key_purge.get(primary)
            if ck:
                purge_set.update(matches.get(ck, []))
            if old_gname in groups:
                groups[old_gname] = [a for a in groups[old_gname] if a not in purge_set]
            if new_gname not in groups:
                groups[new_gname] = []
            if primary not in groups[new_gname]:
                groups[new_gname].append(primary)
                photos_synced += 1

        for primary, gname in diff_added.items():
            if gname not in groups:
                groups[gname] = []
            if primary not in groups[gname]:
                groups[gname].append(primary)
                photos_synced += 1

    # ── Pass E: auto-propagate via match clusters ────────────────────────
    aid_to_group = {}
    for gname, aids in groups.items():
        for aid in aids:
            aid_to_group[aid] = gname

    aid_to_matchkey = {}
    for key, members in matches.items():
        for m in members:
            aid_to_matchkey[m] = key

    # Pass E DISABLED: it appended every cross-stock sibling INTO photo_groups,
    # which bloated groups (and with Freepik started dragging unrelated shoots in).
    # Cross-stock versions now fold into one card at DISPLAY time (api_groups_get
    # uses _cross_stock_matches), so photo_groups must stay the authoritative
    # ms_library shoot membership only. Same reason Pass F's append is gated.
    auto_added = 0

    # ── Re-apply MANUAL group members (survive force rebuilds) ───────────
    # _manual_group_members.json is snapshotted by save_groups() on every UI
    # edit: ids the user added by hand that don't exist in ms_library at all.
    # A force rebuild starts groups from scratch (ms_library only) — without
    # this they'd be silently lost. Groups deleted by the user stay deleted
    # only if their manual ids were removed too (snapshot follows UI saves).
    try:
        from image_utils import load_manual_members
        _manual_restored = 0
        for _mg, _mids in load_manual_members().items():
            cur = groups.setdefault(_mg, [])
            have = set(cur)
            for _mid in _mids:
                if _mid not in have:
                    cur.append(_mid); have.add(_mid); _manual_restored += 1
        if _manual_restored:
            _sync_log(f"♻️ Відновлено {_manual_restored} ручних фото у групах")
    except Exception as _exM:
        _sync_log(f"⚠️ manual members restore: {_exM}")

    # ── Pass G: dedup + SAVE groups (BEFORE the slow Pass F) ─────────────
    # Saving here (not after Pass F) shrinks the lost-update window: a user
    # group edit during the long visual-matching pass would otherwise be
    # overwritten by our stale in-memory snapshot. Pass F only READS `groups`.
    for gname in list(groups.keys()):
        seen: set = set()
        deduped = []
        for aid in groups[gname]:
            if aid not in seen:
                seen.add(aid)
                deduped.append(aid)
        groups[gname] = deduped
    save_groups(groups)

    # ── Pass F: MS+ visual matching → DISPLAY FOLD (cross_stock_matches) ─────
    # Photos sold ONLY on Envato/Freepik/Deposit (no Adobe/SS/iStock counterpart in
    # asset_meta) can't link to their MS+ shoot via Pass A (pHash between sales
    # photos). Pass F matches each sales photo against the MS+ reference thumbnails
    # (ms_meta → ms_library group), then links the matched asset_id to that group's
    # primary stockid IN _cross_stock_matches — so it folds into the group at DISPLAY
    # (api_groups_get), WITHOUT writing into photo_groups (which stays MS+-authoritative).
    #
    # ⚠️ STRICT TIER ONLY (loose_hamming == strict_hamming): the loose tier is what
    # historically bloated a group 339→3495 by dragging wrong shoots in. Strict
    # (hamming ≤ 4 AND RGB ≤ 30) = effectively "the same photo", so cross-stock fold
    # stays clean. Do NOT widen loose_hamming here.
    ms_added = 0
    try:
        _aid_to_grp_f = {m: k for k, members in groups.items() for m in members}
        ms_visual, aid_to_anchor = _ms_visual_matches(
            _aid_to_grp_f,
            strict_hamming=4, loose_hamming=4, loose_rgb_max=20,
            last_asset_meta_rowid=(None if _force else _last_am_rowid),
            last_ms_meta_rowid=(None if _force else _last_mm_rowid))

        # Link each visually-matched sales photo to the SPECIFIC MS+ photo it matched
        # (its anchor id) in _cross_stock_matches → folds onto the correct card with
        # correct earnings. Union-find merge so existing clusters stay intact.
        aid_to_key_f = {m: k for k, members in matches.items() for m in members}
        for aid, anchor in (aid_to_anchor or {}).items():
            aid = str(aid); anchor = str(anchor)
            if not anchor:
                continue
            ka = aid_to_key_f.get(aid)
            kb = aid_to_key_f.get(anchor, anchor)
            if ka == kb:
                continue
            if ka:                                   # merge aid's cluster into anchor's
                merged = sorted(set(matches.get(kb, [kb])) | set(matches.get(ka, [ka])) | {anchor})
                matches[kb] = merged
                for m in matches.get(ka, []):
                    aid_to_key_f[m] = kb
                matches.pop(ka, None)
            else:
                cluster = sorted(set(matches.get(kb, [kb])) | {anchor, aid})
                matches[kb] = cluster
            aid_to_key_f[aid] = kb
            aid_to_key_f[anchor] = kb
            ms_added += 1
        if ms_added:
            _save_matches(matches)
    except Exception as _exF:
        _sync_log(f"⚠️ Pass F (MS+ visual) error: {_exF}")
        ms_added = 0

    # (Pass G moved BEFORE Pass F — groups already deduped + saved above.)

    # ── Pass H: apply manual overrides ───────────────────────────────────
    matches, override_changes = _apply_manual_overrides(matches, overrides)
    _save_matches(matches)

    # ── Persist state ────────────────────────────────────────────────────
    try:
        with open(_state_file, 'w') as _f:
            json.dump({
                'last_asset_meta_rowid':   _cur_am_rowid,
                'last_ms_meta_rowid':      _cur_mm_rowid,
                'last_ms_lib_fingerprint': _cur_lib_fp,
            }, _f)
    except Exception: pass
    try:
        with open(_snapshot_file, 'w') as _f:
            json.dump(ms_authoritative, _f)
    except Exception: pass

    return jsonify({'status': 'ok',
                    'entries': len(matches),
                    'photos': photos_synced,
                    'groups': len(groups),
                    'hash_pairs': hash_pairs,
                    'ms_lib_pairs': ms_added_pairs,
                    'filename_pairs': fn_pairs,
                    'auto_grouped': auto_added,
                    'ms_visual_grouped': ms_added,
                    'override_changes': override_changes})


@matching_bp.route('/api/match-override', methods=['POST'])
def api_match_override():
    """Persist a manual link/unlink that survives future rebuild-matches runs."""
    body    = request.get_json(force=True, silent=True) or {}
    action  = body.get('action')
    primary = str(body.get('primary', '')).strip()
    aid     = str(body.get('asset_id', '')).strip()
    if action not in ('link', 'unlink') or not primary or not aid:
        return jsonify({'ok': False, 'msg': 'need {action: link|unlink, primary, asset_id}'}), 400
    ov = _load_overrides()
    key = 'linked' if action == 'link' else 'unlinked'
    bucket = ov.setdefault(key, {})
    lst = set(bucket.get(primary, []))
    lst.add(aid)
    bucket[primary] = sorted(lst)
    other_key = 'unlinked' if action == 'link' else 'linked'
    other = ov.setdefault(other_key, {})
    if primary in other and aid in other[primary]:
        other[primary] = [x for x in other[primary] if x != aid]
        if not other[primary]:
            other.pop(primary)
    _save_overrides(ov)
    return jsonify({'ok': True, 'action': action, 'primary': primary, 'asset_id': aid})


@matching_bp.route('/api/matches', methods=['GET'])
def api_matches_get():
    """Повертає cross-stock matches."""
    return jsonify(_load_matches())


@matching_bp.route('/api/matches', methods=['POST'])
def api_matches_post():
    """Зберігає cross-stock matches."""
    data = request.get_json(force=True, silent=True) or {}
    _save_matches(data)
    return jsonify({"status": "ok"})
