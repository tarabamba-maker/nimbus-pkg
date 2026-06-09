"""
matching_engine.py — Pure perceptual-hash matching functions.

Moved here from main.py (logic unchanged — only location changed):
  - _hash_based_matches      — pHash clustering with incremental rowid cursor
  - _ms_fname_to_libentry    — disk filename → ms_library entry lookup
  - _ms_visual_matches       — MS+ visual matching (pHash + RGB delta)
  - _filename_fallback_matches — filename-based fallback matcher
  - _apply_manual_overrides  — apply user link/unlink overrides

Flask routes (api_rebuild_matches, api_compute_hashes, api_compute_ms_hashes,
api_refresh_istock_thumbs) stay in main.py.
"""

import os
import sqlite3
import sys

IS_MAC = sys.platform == 'darwin'
IS_WIN = sys.platform == 'win32'

_BASE_DIR = os.environ.get(
    "STOCK_DATA_DIR",
    os.path.dirname(os.path.abspath(__file__))
)
MS_CACHE_DIR = os.path.join(_BASE_DIR, "img_cache_ms")

_RELEVANT_STOCKS = {'adobestock', 'shutterstock', 'istock', 'esp', 'depositphotos'}

from db import DB_NAME
from utils import _hamming_hex, _dhash_from_path
from sync_state import _sync_log, _sync_stop_flag, _sync_state
from image_utils import load_ms_library



def _hash_based_matches(existing_matches, threshold=4, incremental_from_rowid=None):
    """
    Augment existing matches with perceptual-hash-based pairs.

    Full mode (incremental_from_rowid=None): compare all × all asset_meta rows.
    Incremental mode: only compare pairs where at least ONE side has rowid >
    incremental_from_rowid. Pairs of two "old" rows were already processed in
    a prior rebuild and merged into `existing_matches` — reprocessing them is
    wasted work. Same matching threshold, same merge logic. Drops from O(n²)
    to O(n·k) where k = count of new rows.

    Returns (matches, new_pairs, max_rowid).
    """
    with sqlite3.connect(DB_NAME, timeout=15) as c:
        rows = c.execute(
            "SELECT rowid, stock, asset_id, thumb_hash, aspect_ratio, r, g, b FROM asset_meta "
            "WHERE thumb_hash IS NOT NULL AND thumb_hash != ''"
        ).fetchall()
    if not rows:
        return existing_matches, 0, 0

    max_rowid = max(r[0] for r in rows)
    by_aid = {aid: (stock, h, ar, r, g, b)
              for _rid, stock, aid, h, ar, r, g, b in rows}
    new_aids = (
        {aid for rid, _s, aid, *_ in rows if rid > incremental_from_rowid}
        if incremental_from_rowid is not None else None
    )

    # Build aid -> set(group members) from existing matches for fast merging
    aid_to_group_key = {}
    for primary, members in existing_matches.items():
        for m in members:
            aid_to_group_key[m] = primary

    new_pairs = 0
    aids = list(by_aid.keys())

    def _merge(aid_a, aid_b):
        """Union-find merge of a matched pair into existing_matches. Identical
        verdict + merge as the original per-pair logic; only the candidate SEARCH
        is vectorised below. Returns True if it counted a new pair."""
        nonlocal new_pairs
        ka = aid_to_group_key.get(aid_a)
        kb = aid_to_group_key.get(aid_b)
        if ka and kb:
            if ka == kb:
                return
            existing_matches[ka] = sorted(set(existing_matches.get(ka, []) + existing_matches.get(kb, [])))
            for m in existing_matches.get(kb, []):
                aid_to_group_key[m] = ka
            existing_matches.pop(kb, None)
        elif ka:
            existing_matches[ka] = sorted(set(existing_matches[ka] + [aid_b]))
            aid_to_group_key[aid_b] = ka
        elif kb:
            existing_matches[kb] = sorted(set(existing_matches[kb] + [aid_a]))
            aid_to_group_key[aid_a] = kb
        else:
            existing_matches[aid_a] = sorted([aid_a, aid_b])
            aid_to_group_key[aid_a] = aid_a
            aid_to_group_key[aid_b] = aid_a
        new_pairs += 1

    # ── VECTORISED candidate search (numpy): hamming via XOR+popcount on uint64
    # arrays + masked stock/aspect/RGB filters. Turns the O(n²) pure-Python pair
    # scan (minutes on 35k rows) into ~seconds. SAME match verdict as before. ──
    try:
        import numpy as np
        _have_np = True
    except Exception:
        _have_np = False

    if _have_np and len(aids) > 1:
        n = len(aids)
        idxmap = {a: i for i, a in enumerate(aids)}
        H  = np.zeros(n, dtype=np.uint64)
        AR = np.full(n, -1.0, dtype=np.float64)
        R  = np.zeros(n, dtype=np.int64); G = np.zeros(n, dtype=np.int64); B = np.zeros(n, dtype=np.int64)
        has_rgb = np.zeros(n, dtype=bool)
        S  = np.zeros(n, dtype=np.int64)
        _scode: dict = {}
        for i, a in enumerate(aids):
            stock, h, ar, r, g, b = by_aid[a]
            try:
                H[i] = np.uint64(int(h, 16) & 0xFFFFFFFFFFFFFFFF)
            except Exception:
                H[i] = np.uint64(0)
            if ar:
                AR[i] = float(ar)
            if isinstance(r, int) and isinstance(g, int) and isinstance(b, int):
                R[i] = r; G[i] = g; B[i] = b; has_rgb[i] = True
            S[i] = _scode.setdefault(stock, len(_scode))

        _POP = np.array([bin(x).count('1') for x in range(256)], dtype=np.uint8)
        def _popcount(u64arr):
            return _POP[u64arr.view(np.uint8).reshape(-1, 8)].sum(axis=1)

        full = new_aids is None
        rows_to_scan = range(n) if full else [idxmap[a] for a in aids if a in new_aids]

        for i in rows_to_scan:
            cand = _popcount(H ^ H[i]) <= threshold       # hamming ≤ threshold
            cand[i] = False
            cand &= (S != S[i])                            # different stock
            if AR[i] >= 0:                                 # aspect within 0.05 (if both have it)
                cand &= ((AR < 0) | (np.abs(AR - AR[i]) <= 0.05))
            if has_rgb[i]:                                 # RGB L1 ≤ 45 (if both have it)
                l1 = np.abs(R - R[i]) + np.abs(G - G[i]) + np.abs(B - B[i])
                cand &= (~has_rgb | (l1 <= 45))
            if full:
                cand[:i + 1] = False                       # only j>i: process each pair once
            for j in np.nonzero(cand)[0]:
                _merge(aids[i], aids[int(j)])
        return existing_matches, new_pairs, max_rowid

    # ── fallback: original pure-Python pair scan (numpy unavailable) ──
    def _pair_iter():
        if new_aids is None:
            for i, a in enumerate(aids):
                for b in aids[i+1:]:
                    yield a, b
            return
        new_list = [a for a in aids if a in new_aids]
        old_list = [a for a in aids if a not in new_aids]
        for i, na in enumerate(new_list):
            for nb in new_list[i+1:]:
                yield na, nb
            for ob in old_list:
                yield na, ob

    for aid_a, aid_b in _pair_iter():
        sa, ha, ara, ra, ga, ba = by_aid[aid_a]
        sb, hb, arb, rb, gb, bb = by_aid[aid_b]
        if sa == sb:
            continue
        if ara and arb and abs(ara - arb) > 0.05:
            continue
        if _hamming_hex(ha, hb) > threshold:
            continue
        try:
            if (isinstance(ra, int) and isinstance(rb, int) and
                isinstance(ga, int) and isinstance(gb, int) and
                isinstance(ba, int) and isinstance(bb, int) and
                (abs(ra - rb) + abs(ga - gb) + abs(ba - bb)) > 45):
                continue
        except Exception:
            pass
        _merge(aid_a, aid_b)
    return existing_matches, new_pairs, max_rowid


def _ms_fname_to_libentry(disk_base, lib_filenames):
    """
    Map disk filename (without .jpg, e.g. 'B94A7017_45908268') to its base
    ms_library filename by stripping common variant suffixes.
    Returns the base name if found in lib_filenames, else None.
    """
    if disk_base in lib_filenames:
        return disk_base
    # _<digits> suffix (stock ID variants like _45908268)
    if '_' in disk_base:
        base = disk_base.rsplit('_', 1)[0]
        if base in lib_filenames:
            return base
    # -<word> suffix (e.g. -flipHorizontal, -bw, -1, -2)
    if '-' in disk_base:
        base = disk_base.rsplit('-', 1)[0]
        if base in lib_filenames:
            return base
    return None


def _ms_visual_matches(aid_to_group, strict_hamming=4, loose_hamming=8, loose_rgb_max=20,
                       last_asset_meta_rowid=None, last_ms_meta_rowid=None):
    """
    Two-tier MS+ visual matching for sales aids without a group.

    Incremental mode: if either cursor is set, a pair (sale × ms) is processed
    only when at least ONE side has rowid > last cursor. Pairs of two "old"
    rows were already evaluated in a prior rebuild — their outcome is
    deterministic (thresholds + hash values are immutable), so reprocessing
    them yields the same verdict. Drops from 60M comparisons to k*15k+10k*j.

    aid_to_group: existing {aid -> gname} map (mutated for new assignments)
    Returns: {gname: [new_aids...]} additions to apply to photo_groups.
    """
    lib = load_ms_library()
    # NEW: build basepath-based lookup. Disk filenames are derived from basepath
    # as "/A/B/file" → "A__B__file.jpg", so we map back the same way. Falls back
    # to filename for legacy disk entries from older builds.
    bp_to_group = {}
    fname_to_group = {}  # legacy fallback
    bp_to_disk = {}      # basepath → expected disk base name (no .jpg)
    for p in lib:
        bp = (p.get('basepath') or '').strip()
        fn = (p.get('filename') or '').strip()
        gn = (p.get('group') or '').strip()
        if bp and gn:
            bp_to_group[bp] = gn
            disk_base = bp.lstrip("/").replace("/", "__").replace("\\", "__")
            bp_to_disk[disk_base] = bp
        if fn and gn:
            fname_to_group[fn] = gn
    if not bp_to_group and not fname_to_group:
        return {}

    lib_filenames = set(fname_to_group.keys())

    with sqlite3.connect(DB_NAME, timeout=15) as c:
        sales_rows = c.execute(
            "SELECT rowid, stock, asset_id, thumb_hash, aspect_ratio, r, g, b FROM asset_meta "
            "WHERE thumb_hash IS NOT NULL AND thumb_hash != ''"
        ).fetchall()
        ms_rows = c.execute(
            "SELECT rowid, fname, thumb_hash, aspect_ratio, r, g, b FROM ms_meta "
            "WHERE thumb_hash IS NOT NULL AND thumb_hash != ''"
        ).fetchall()
    new_sale_aids = (
        {str(aid) for rid, _s, aid, *_ in sales_rows if rid > last_asset_meta_rowid}
        if last_asset_meta_rowid is not None else None
    )
    new_ms_fnames = (
        {fn for rid, fn, *_ in ms_rows if rid > last_ms_meta_rowid}
        if last_ms_meta_rowid is not None else None
    )
    # Strip rowid from rows for downstream code (was originally 7/6 columns).
    sales_rows = [(s, a, h, ar, r, g, b) for _rid, s, a, h, ar, r, g, b in sales_rows]
    ms_rows    = [(fn, h, ar, r, g, b)    for _rid, fn, h, ar, r, g, b in ms_rows]

    if not ms_rows:
        return {}

    # Resolve each disk fname → group. First try basepath-derived disk name
    # (new format), fall back to legacy filename lookup.
    ms_resolved = []
    for fname, h, ar, r, g, b in ms_rows:
        gn = None
        if fname in bp_to_disk:
            gn = bp_to_group.get(bp_to_disk[fname])
        if not gn:
            base = _ms_fname_to_libentry(fname, lib_filenames)
            if base:
                gn = fname_to_group.get(base)
        if not gn:
            continue
        is_new_ms = (new_ms_fnames is None) or (fname in new_ms_fnames)
        ms_resolved.append((gn, h, int(h, 16), ar, r, g, b, is_new_ms))

    additions = {}
    for stock, aid, ha, ara, ra, ga, ba in sales_rows:
        aid = str(aid)
        if aid in aid_to_group:
            continue  # already grouped — skip
        is_new_sale = (new_sale_aids is None) or (aid in new_sale_aids)
        try:
            ha_int = int(ha, 16)
        except Exception:
            continue
        best = None  # (hamming, gname)
        for gn, hb, hb_int, arb, rb, gb, bb, is_new_ms in ms_resolved:
            # Incremental: skip if BOTH sides are old (already evaluated in
            # a previous rebuild — same hashes + same thresholds → same verdict).
            if not is_new_sale and not is_new_ms:
                continue
            if ara and arb and abs(ara - arb) > 0.15:
                continue
            # fast hamming via xor + popcount
            d = bin(ha_int ^ hb_int).count('1')
            if d > loose_hamming:
                continue
            try:
                if not (isinstance(ra, int) and isinstance(rb, int) and
                        isinstance(ga, int) and isinstance(gb, int) and
                        isinstance(ba, int) and isinstance(bb, int)):
                    rgb_dist = None
                else:
                    rgb_dist = abs(ra - rb) + abs(ga - gb) + abs(ba - bb)
            except Exception:
                rgb_dist = None
            # Tiered acceptance — both clean, identical photos give hamming 0-2
            if d <= strict_hamming:
                if rgb_dist is not None and rgb_dist > 30:
                    continue
            else:
                # loose tier — only accept if colors are near-identical
                if rgb_dist is None or rgb_dist > loose_rgb_max:
                    continue
            if best is None or d < best[0]:
                best = (d, gn)
                if d == 0:
                    break  # perfect match, no need to keep looking
        if best:
            gn = best[1]
            additions.setdefault(gn, []).append(aid)
            aid_to_group[aid] = gn
    return additions


def _filename_fallback_matches(matches, lib, threshold_hamming=4, threshold_rgb=30):
    """Pass C: filename fallback.
    For each asset NOT yet in any cluster, look up ms_library entries with the
    same filename basename. Candidates from ms_library.stockids are VERIFIED via
    pHash+RGB (camera reuses filenames every ~10k photos — name alone is unreliable).
    """
    # Build aid → cluster_key index
    aid_to_key = {m: k for k, members in matches.items() for m in members}

    # ms_library: filename → set of candidate stockids
    fn_to_candidates = {}
    for photo in lib:
        fn = (photo.get('filename') or '').strip()
        if not fn:
            continue
        sids = photo.get('stockids') or {}
        cand = {str(v) for k, v in sids.items() if k in _RELEVANT_STOCKS and v}
        if not cand:
            continue
        fn_to_candidates.setdefault(fn, set()).update(cand)

    # Pull asset_meta + sales.filename for unmatched assets
    with sqlite3.connect(DB_NAME, timeout=15) as c:
        rows = c.execute("""
            SELECT s.stock, s.asset_id, MAX(s.filename), m.thumb_hash, m.aspect_ratio, m.r, m.g, m.b
              FROM sales s LEFT JOIN asset_meta m
                ON m.stock=s.stock AND m.asset_id=s.asset_id
             WHERE s.filename IS NOT NULL AND s.filename != ''
               AND m.thumb_hash IS NOT NULL
             GROUP BY s.stock, s.asset_id
        """).fetchall()
        # Also fetch candidate meta in one query
        cand_meta_cache = {}
        def _meta(aid):
            if aid in cand_meta_cache:
                return cand_meta_cache[aid]
            row = c.execute(
                "SELECT stock, thumb_hash, aspect_ratio, r, g, b FROM asset_meta WHERE asset_id=?",
                (str(aid),)).fetchone()
            cand_meta_cache[aid] = row
            return row

        new_pairs = 0
        for stock, aid, fn, h, ar, r, g, b in rows:
            if aid in aid_to_key:
                continue   # already matched
            fn_clean = (fn or '').rsplit('.', 1)[0]   # strip extension if any
            cands = fn_to_candidates.get(fn_clean, set())
            if not cands:
                continue
            for cand_aid in cands:
                if cand_aid == aid:
                    continue
                meta = _meta(cand_aid)
                if not meta or not meta[1]:
                    continue
                c_stock, ch, car, cr, cg, cb = meta
                if c_stock == stock:
                    continue
                # Stricter aspect_ratio — camera filename collisions across shoots
                # can pass loose pHash; aspect must match closely too.
                if ar and car and abs(ar - car) > 0.05:
                    continue
                if _hamming_hex(h, ch) > threshold_hamming:
                    continue
                if (isinstance(r, int) and isinstance(cr, int) and
                    isinstance(g, int) and isinstance(cg, int) and
                    isinstance(b, int) and isinstance(cb, int) and
                    abs(r - cr) + abs(g - cg) + abs(b - cb) > threshold_rgb):
                    continue
                # Verified — merge into cluster
                ka, kb = aid_to_key.get(aid), aid_to_key.get(cand_aid)
                if ka and kb:
                    if ka == kb:
                        continue
                    matches[ka] = sorted(set(matches.get(ka, []) + matches.get(kb, [])))
                    for m in matches.get(kb, []):
                        aid_to_key[m] = ka
                    matches.pop(kb, None)
                elif ka:
                    matches[ka] = sorted(set(matches[ka] + [cand_aid]))
                    aid_to_key[cand_aid] = ka
                elif kb:
                    matches[kb] = sorted(set(matches[kb] + [aid]))
                    aid_to_key[aid] = kb
                else:
                    matches[aid] = sorted([aid, cand_aid])
                    aid_to_key[aid] = aid
                    aid_to_key[cand_aid] = aid
                new_pairs += 1
                break   # one verified match per asset is enough
    return matches, new_pairs


def _apply_manual_overrides(matches, overrides):
    """Pass H: force-apply user-edited link/unlink. Wins over auto-matching."""
    if not overrides:
        return matches, 0
    linked   = overrides.get('linked', {}) or {}
    unlinked = overrides.get('unlinked', {}) or {}
    changes = 0
    # Remove unlinked members
    for pk, removed in unlinked.items():
        if pk in matches:
            before = len(matches[pk])
            matches[pk] = [m for m in matches[pk] if m not in set(removed)]
            changes += before - len(matches[pk])
    # Add linked members (create cluster if absent)
    for pk, added in linked.items():
        if pk not in matches:
            matches[pk] = [pk]
        existing = set(matches[pk])
        for a in added:
            if a not in existing:
                matches[pk].append(a)
                existing.add(a)
                changes += 1
        matches[pk] = sorted(existing)
    return matches, changes

