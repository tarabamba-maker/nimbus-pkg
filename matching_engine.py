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


# ── Multiprocessing pHash candidate search ────────────────────────────────────
# The candidate SEARCH (XOR+popcount + stock/aspect/RGB masks per row) is the only
# CPU-heavy part of rebuild-matches and is single-core under the GIL. We fan it out
# across all cores with multiprocessing: each worker scans a slice of rows and
# returns the (i, j) candidate pairs. The order-dependent union-find _merge stays
# SEQUENTIAL in the parent over the pairs sorted by (i, j) — exactly the order the
# single-process loop produced — so the verdict + new_pairs count are IDENTICAL.
# Read-only arrays are shared via a per-worker initializer (tiny: ~0.3 MB each).
_MPW: dict = {}


def _mp_init(H, AR, R, G, B, has_rgb, S, POP, threshold, full):
    _MPW.update(H=H, AR=AR, R=R, G=G, B=B, has_rgb=has_rgb, S=S, POP=POP,
                threshold=threshold, full=full)


def _mp_scan(row_indices):
    """Worker: return [(i, j), …] candidate pairs for the given rows. Same mask
    logic as the in-process vectorised loop."""
    import numpy as np
    H = _MPW['H']; AR = _MPW['AR']; R = _MPW['R']; G = _MPW['G']; B = _MPW['B']
    has_rgb = _MPW['has_rgb']; S = _MPW['S']; POP = _MPW['POP']
    threshold = _MPW['threshold']; full = _MPW['full']
    def popcount(u):
        return POP[u.view(np.uint8).reshape(-1, 8)].sum(axis=1)
    out = []
    for i in row_indices:
        cand = popcount(H ^ H[i]) <= threshold
        cand[i] = False
        cand &= (S != S[i])
        if AR[i] >= 0:
            cand &= ((AR < 0) | (np.abs(AR - AR[i]) <= 0.05))
        if has_rgb[i]:
            l1 = np.abs(R - R[i]) + np.abs(G - G[i]) + np.abs(B - B[i])
            cand &= (~has_rgb | (l1 <= 45))
        if full:
            cand[:i + 1] = False
        for j in np.nonzero(cand)[0]:
            out.append((i, int(j)))
    return out


# ── Multiprocessing MS+ visual matching (Pass F) ──────────────────────────────
# Each unmatched sale independently finds its best MS+ group — no interaction
# between sales — so it's embarrassingly parallel. Workers scan a slice of sales
# against the shared ms_resolved table and return (idx, aid, gname) for matches;
# the parent applies them in idx (original-sales) order with an aid-dedup guard,
# giving a result identical to the single-core loop.
_MSVW: dict = {}


def _msv_init(ms_resolved, strict_hamming, loose_hamming, loose_rgb_max):
    _MSVW.update(ms=ms_resolved, sh=strict_hamming, lh=loose_hamming, lrgb=loose_rgb_max)


def _msv_scan(chunk):
    ms = _MSVW['ms']; sh = _MSVW['sh']; lh = _MSVW['lh']; lrgb = _MSVW['lrgb']
    out = []
    for idx, aid, ha_int, ara, ra, ga, ba, is_new_sale in chunk:
        best = None
        for gn, anc, hb, hb_int, arb, rb, gb, bb, is_new_ms in ms:
            # Incremental: skip pairs where BOTH sides are old (already evaluated).
            if not is_new_sale and not is_new_ms:
                continue
            if ara and arb and abs(ara - arb) > 0.15:
                continue
            d = bin(ha_int ^ hb_int).count('1')
            if d > lh:
                continue
            if (isinstance(ra, int) and isinstance(rb, int) and isinstance(ga, int)
                    and isinstance(gb, int) and isinstance(ba, int) and isinstance(bb, int)):
                rgb_dist = abs(ra - rb) + abs(ga - gb) + abs(ba - bb)
            else:
                rgb_dist = None
            if d <= sh:
                if rgb_dist is not None and rgb_dist > 30:
                    continue
            else:
                if rgb_dist is None or rgb_dist > lrgb:
                    continue
            if best is None or d < best[0]:
                best = (d, gn, anc)
                if d == 0:
                    break
        if best:
            out.append((idx, aid, best[1], best[2]))
    return out



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
        rows_to_scan = list(range(n)) if full else [idxmap[a] for a in aids if a in new_aids]

        # ⚠️ Multiprocessing DISABLED. A spawn Pool deadlocks inside the app-launched
        # Flask server (workers wedge at 0% CPU forever → the whole rebuild hangs).
        # Speed comes from LSH bucketing instead (below) — single-core, deterministic.
        #
        # LSH / pigeonhole bucketing: split the 64-bit hash into (threshold+1)
        # disjoint bit-chunks. If hamming(a,b) ≤ threshold, at LEAST one chunk is
        # bitwise IDENTICAL (pigeonhole) — so the true candidate set is fully
        # contained in the union of same-chunk buckets. EXACT same verdict as the
        # full scan, but each row compares against ~hundreds instead of all n.
        nchunks = threshold + 1
        cbits = 64 // nchunks
        chunk_vals = []          # per chunk: int array of that chunk's bits
        buckets = []             # per chunk: {value: [row indices]}
        for c in range(nchunks):
            shift = np.uint64(c * cbits)
            width = 64 - c * cbits if c == nchunks - 1 else cbits
            mask = np.uint64((1 << width) - 1)
            vals = ((H >> shift) & mask).astype(np.int64)
            chunk_vals.append(vals)
            d: dict = {}
            for i, v in enumerate(vals.tolist()):
                d.setdefault(v, []).append(i)
            buckets.append(d)

        _sync_log(f"⚙️ pHash matching (LSH): {len(rows_to_scan)} рядків…")
        for i in rows_to_scan:
            cset: set = set()
            for c in range(nchunks):
                cset.update(buckets[c].get(int(chunk_vals[c][i]), ()))
            cset.discard(i)
            if not cset:
                continue
            idx = np.fromiter(cset, dtype=np.int64, count=len(cset))
            cand = _popcount(H[idx] ^ H[i]) <= threshold   # hamming ≤ threshold
            cand &= (S[idx] != S[i])                        # different stock
            if AR[i] >= 0:                                  # aspect within 0.05 (if both have it)
                cand &= ((AR[idx] < 0) | (np.abs(AR[idx] - AR[i]) <= 0.05))
            if has_rgb[i]:                                  # RGB L1 ≤ 45 (if both have it)
                l1 = np.abs(R[idx] - R[i]) + np.abs(G[idx] - G[i]) + np.abs(B[idx] - B[i])
                cand &= (~has_rgb[idx] | (l1 <= 45))
            if full:
                cand &= (idx > i)                           # only j>i: process each pair once
            for j in idx[cand]:
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
    # `*_to_anchor`: the MS+ photo's canonical id (its first stockid). This is NOT
    # stockid-based MATCHING (matching is 100% pHash+RGB below) — it's only the join
    # key the visual match folds onto, so each photo lands on the CORRECT card.
    # ⚠️ MUST mirror routes/matching.py _pick_primary (_PRIMARY_PRIORITY): Pass D
    # stores THAT primary in photo_groups. If the anchor is a different stockid
    # (e.g. dict-order first = alamy), Pass F clusters the sale onto an id the
    # group doesn't contain → the group shows $0 (the "Cooking home" bug).
    _ANCHOR_PRIORITY = ['adobestock', 'shutterstock', 'istock', 'esp', 'depositphotos']
    def _anchor_of(stockids):
        sids = stockids or {}
        for key in _ANCHOR_PRIORITY:
            v = sids.get(key)
            if v:
                return str(v)
        vals = [str(v) for v in sids.values() if v]
        return vals[0] if vals else ''
    bp_to_group = {}
    fname_to_group = {}  # legacy fallback
    bp_to_disk = {}      # basepath → expected disk base name (no .jpg)
    bp_to_anchor = {}
    fname_to_anchor = {}
    for p in lib:
        bp = (p.get('basepath') or '').strip()
        fn = (p.get('filename') or '').strip()
        gn = (p.get('group') or '').strip()
        anc = _anchor_of(p.get('stockids'))
        if bp and gn:
            bp_to_group[bp] = gn
            disk_base = bp.lstrip("/").replace("/", "__").replace("\\", "__")
            bp_to_disk[disk_base] = bp
            if anc: bp_to_anchor[bp] = anc
        if fn and gn:
            fname_to_group[fn] = gn
            if anc: fname_to_anchor[fn] = anc
    if not bp_to_group and not fname_to_group:
        return {}, {}

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
        return {}, {}

    # Resolve each disk fname → group + the MS+ photo's anchor id. The anchor lets
    # Pass F link a matched sales photo to the SPECIFIC MS+ photo it matched (so it
    # folds onto the right card with the right earnings), not a per-group anchor.
    ms_resolved = []
    for fname, h, ar, r, g, b in ms_rows:
        gn = None; anc = ''
        if fname in bp_to_disk:
            bp = bp_to_disk[fname]
            gn = bp_to_group.get(bp); anc = bp_to_anchor.get(bp, '')
        if not gn:
            base = _ms_fname_to_libentry(fname, lib_filenames)
            if base:
                gn = fname_to_group.get(base); anc = fname_to_anchor.get(base, '')
        if not gn:
            continue
        is_new_ms = (new_ms_fnames is None) or (fname in new_ms_fnames)
        ms_resolved.append((gn, anc, h, int(h, 16), ar, r, g, b, is_new_ms))

    additions = {}
    aid_to_anchor = {}    # aid → matched MS+ photo's canonical id (for the display fold)

    # ── numpy-vectorised single-core path ────────────────────────────────────
    # For each sales photo, popcount(MS_hashes XOR sale) + RGB/aspect masks in one
    # vectorised pass over all ms_meta. ~seconds on 33k×15k. NO multiprocessing here:
    # a SECOND spawn Pool (after Pass A's) deadlocked inside the app-launched Flask
    # server (workers wedged at 0% CPU forever). numpy keeps it fast AND reliable.
    try:
        import numpy as np
        _have_np = True
    except Exception:
        _have_np = False

    if _have_np and ms_resolved:
        m = len(ms_resolved)
        MH = np.zeros(m, dtype=np.uint64)
        MAR = np.full(m, -1.0, dtype=np.float64)
        MR = np.zeros(m, np.int64); MG = np.zeros(m, np.int64); MB = np.zeros(m, np.int64)
        MNEW = np.zeros(m, bool); has_rgb_ms = np.zeros(m, bool)
        MGN = [None] * m; MANC = [None] * m
        for i, (gn, anc, h, hi, ar, r, g, b, is_new_ms) in enumerate(ms_resolved):
            MH[i] = np.uint64(hi & 0xFFFFFFFFFFFFFFFF)
            if ar: MAR[i] = float(ar)
            if isinstance(r, int) and isinstance(g, int) and isinstance(b, int):
                MR[i] = r; MG[i] = g; MB[i] = b; has_rgb_ms[i] = True
            MNEW[i] = bool(is_new_ms); MGN[i] = gn; MANC[i] = anc
        _POP = np.array([bin(x).count('1') for x in range(256)], dtype=np.uint8)
        def _popc(arr): return _POP[arr.view(np.uint8).reshape(-1, 8)].sum(axis=1)

        # LSH / pigeonhole bucketing over the MS+ hashes (same trick as Pass A):
        # split 64 bits into (loose_hamming+1) chunks; any MS+ hash within
        # loose_hamming of the sale hash MUST share at least one exact chunk.
        # Exact same verdict, but each sale compares vs ~tens instead of all m.
        nchunks = loose_hamming + 1
        cbits = 64 // nchunks
        _chunk_meta = []          # (shift, mask) per chunk
        ms_buckets = []           # per chunk: {value: np.array(row indices)}
        for c in range(nchunks):
            shift = np.uint64(c * cbits)
            width = 64 - c * cbits if c == nchunks - 1 else cbits
            mask = np.uint64((1 << width) - 1)
            vals = ((MH >> shift) & mask).astype(np.int64)
            d: dict = {}
            for i, v in enumerate(vals.tolist()):
                d.setdefault(v, []).append(i)
            ms_buckets.append(d)
            _chunk_meta.append((int(c * cbits), int(mask)))

        _sync_log(f"⚙️ MS+ visual matching (numpy+LSH): {len(sales_rows)}×{m}…")
        for stock, aid, ha, ara, ra, ga, ba in sales_rows:
            aid = str(aid)
            if aid in aid_to_group:
                continue
            is_new_sale = (new_sale_aids is None) or (aid in new_sale_aids)
            try:
                ha_int = int(ha, 16)
            except Exception:
                continue
            ha_int &= 0xFFFFFFFFFFFFFFFF
            cset: set = set()
            for c in range(nchunks):
                shift, mask = _chunk_meta[c]
                cset.update(ms_buckets[c].get((ha_int >> shift) & mask, ()))
            if not cset:
                continue
            idx = np.fromiter(cset, dtype=np.int64, count=len(cset))
            pop = _popc(MH[idx] ^ np.uint64(ha_int))
            cand = pop <= loose_hamming
            if not is_new_sale:
                cand &= MNEW[idx]                  # incremental: ≥1 side new
            if ara:
                cand &= ((MAR[idx] < 0) | (np.abs(MAR[idx] - ara) <= 0.15))
            if isinstance(ra, int) and isinstance(ga, int) and isinstance(ba, int):
                rgb = np.abs(MR[idx] - ra) + np.abs(MG[idx] - ga) + np.abs(MB[idx] - ba)
                strict_ok = (pop <= strict_hamming) & ((~has_rgb_ms[idx]) | (rgb <= 30))
                loose_ok  = (pop > strict_hamming) & has_rgb_ms[idx] & (rgb <= loose_rgb_max)
            else:
                strict_ok = (pop <= strict_hamming)    # no sale RGB → strict on hamming only
                loose_ok  = np.zeros(len(idx), bool)   # loose needs RGB → reject
            cand &= (strict_ok | loose_ok)
            if not cand.any():
                continue
            sub = np.nonzero(cand)[0]
            # best = min hamming; tie-break on ORIGINAL row order (j) so the verdict
            # is identical to the pre-LSH full scan (np.argmin took the first row).
            jbest = min((int(pop[s]), int(idx[s])) for s in sub)[1]
            additions.setdefault(MGN[jbest], []).append(aid)
            aid_to_group[aid] = MGN[jbest]
            if MANC[jbest]:
                aid_to_anchor[aid] = MANC[jbest]
        return additions, aid_to_anchor

    # ── pure-Python fallback (numpy unavailable) ──
    for stock, aid, ha, ara, ra, ga, ba in sales_rows:
        aid = str(aid)
        if aid in aid_to_group:
            continue  # already grouped — skip
        is_new_sale = (new_sale_aids is None) or (aid in new_sale_aids)
        try:
            ha_int = int(ha, 16)
        except Exception:
            continue
        best = None  # (hamming, gname, anchor)
        for gn, anc, hb, hb_int, arb, rb, gb, bb, is_new_ms in ms_resolved:
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
                best = (d, gn, anc)
                if d == 0:
                    break  # perfect match, no need to keep looking
        if best:
            additions.setdefault(best[1], []).append(aid)
            aid_to_group[aid] = best[1]
            if best[2]: aid_to_anchor[aid] = best[2]
    return additions, aid_to_anchor


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



# Per-stock hamming threshold to the MS+ ROOT. Matching is hub-and-spoke: every
# stock matches to its MS+ reference, never stock-to-stock. Some stocks generate
# their thumbnail with a DIFFERENT crop/processing than the MS+ reference (PIXTA
# re-crops + letterboxes), so their dHash sits a consistent ~1 bit further out and
# misses the strict ≤4. Calibrate each such stock to its own offset. Verified safe:
# PIXIA ungrouped sweep → 0 ambiguous up to hamming 7. Default stays strict (4).
_STOCK_MSPLUS_HAM = {"PIXTA": 6}


def _basepath_group_fallback(grouped_ids, aliases=None,
                             ham_max=4, rgb_max=30, aspect_max=0.05,
                             stock_ham=None):
    """FALLBACK (pHash + MS+ basepath) for SALES that ended up with NO group.

    Sales whose MS+ reference carries no stockids (recolors, orphan ms_meta) never
    get a group via the stockid-anchored Pass D / Pass F. Here we match each such
    sale to its MS+ reference by pHash, and read the SHOOT straight from the
    reference's basepath (ms_meta fname = "year__group__file"), alias-resolved.

    MS+ stays the source of truth — the group is the MS+ basepath, NOT a stockid
    (stockids are unreliable: the camera repeats basenames every ~10k frames and
    stockids don't always match MS+). The hamming budget is PER STOCK (`stock_ham`,
    default `_STOCK_MSPLUS_HAM`) because each stock has its own crop/thumbnail offset
    from the MS+ root. ONLY ungrouped sales are touched; an ambiguous match (2+
    distinct shoots) is skipped, never guessed.

    Returns {group_name: [asset_id, ...]} additions to append to photo_groups.
    """
    import numpy as np
    aliases = aliases or {}
    stock_ham = _STOCK_MSPLUS_HAM if stock_ham is None else stock_ham
    grouped = {str(x) for x in grouped_ids}

    def _grp(fname):
        parts = fname.split('__')
        g = parts[-2].strip() if len(parts) >= 2 else ''
        seen = set()                       # follow alias chain, cycle-guarded
        while g in aliases and g not in seen:
            seen.add(g)
            g = (aliases[g] or '').strip()
        return g

    with sqlite3.connect(DB_NAME, timeout=15) as c:
        sales = c.execute(
            "SELECT m.asset_id, m.stock, m.thumb_hash, m.aspect_ratio, m.r, m.g, m.b "
            "FROM asset_meta m "
            "WHERE m.thumb_hash IS NOT NULL AND m.thumb_hash != '' "
            "AND m.asset_id IN (SELECT asset_id FROM sales)").fetchall()
        ms = c.execute(
            "SELECT fname, thumb_hash, aspect_ratio, r, g, b FROM ms_meta "
            "WHERE thumb_hash IS NOT NULL AND thumb_hash != ''").fetchall()
    if not sales or not ms:
        return {}

    def _u64(h):
        try:
            return np.uint64(int(h, 16))
        except Exception:
            return np.uint64(0)

    mh = np.array([_u64(r[1]) for r in ms], dtype=np.uint64)
    mar = np.array([r[2] if r[2] is not None else -1 for r in ms], dtype=np.float32)
    mrgb = np.array([[r[3] or 0, r[4] or 0, r[5] or 0] for r in ms], dtype=np.int32)
    mgrp = [_grp(r[0]) for r in ms]
    POP = np.array([bin(i).count('1') for i in range(256)], dtype=np.uint8)

    def popc(x):
        return POP[x.view(np.uint8).reshape(-1, 8)].sum(1).astype(np.int16)

    additions: dict = {}
    for aid, stock, h, ar, r, g, b in sales:
        aid = str(aid)
        if aid in grouped:
            continue
        hmax = stock_ham.get(stock, ham_max)
        ham = popc(mh ^ np.uint64(_u64(h)))
        mask = ham <= hmax
        if ar is not None:
            mask &= (mar < 0) | (np.abs(mar - ar) <= aspect_max)
        mask &= np.abs(mrgb - np.array([r or 0, g or 0, b or 0], dtype=np.int32)).sum(1) <= rgb_max
        idx = np.where(mask)[0]
        if len(idx) == 0:
            continue
        cg = {mgrp[i] for i in idx if mgrp[i]}
        if len(cg) != 1:                       # ambiguous / no group → skip, never guess
            continue
        additions.setdefault(next(iter(cg)), []).append(aid)
    return additions
