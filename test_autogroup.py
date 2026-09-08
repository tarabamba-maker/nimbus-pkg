#!/usr/bin/env python3
"""
test_autogroup.py — DRY RUN (writes nothing).

Tests the pHash+basepath auto-grouping FALLBACK for sales that currently have NO
group. For each ungrouped sale it pHash-matches against ms_meta references and
derives the group from the reference's basepath (fname = year__group__file), then
alias-resolves it. Reports accuracy so we can decide if it's safe to wire in.

MS+ groups stay the source of truth; this only ever targets ungrouped sales.
"""
import os, json, sqlite3
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
SUPPORT = os.path.expanduser("~/Library/Application Support/StockAutomation")
RECIPES = os.path.join(SUPPORT, "recipes")
DB = os.path.join(SUPPORT, "sales.db")

HAM_MAX = 4          # strict tier (same as Pass F strict_hamming)
RGB_MAX = 30
ASPECT_MAX = 0.05

_POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def popcount(x):
    x = x.view(np.uint8).reshape(-1, 8)
    return _POP[x].sum(1).astype(np.int16)


def h2u(s):
    try:
        return np.uint64(int(s, 16))
    except Exception:
        return np.uint64(0)


def group_from_fname(fname):
    parts = fname.split("__")
    return parts[-2].strip() if len(parts) >= 2 else ""


def main():
    groups = json.load(open(os.path.join(RECIPES, "photo_groups.json")))
    try:
        aliases = json.load(open(os.path.join(RECIPES, "_group_aliases.json")))
    except Exception:
        aliases = {}
    try:
        xmatch = json.load(open(os.path.join(RECIPES, "_cross_stock_matches.json")))
    except Exception:
        xmatch = {}

    grouped = set()
    for members in groups.values():
        grouped.update(members)
    # a sale folded via cross_stock onto a grouped primary counts as grouped
    for prim, sibs in xmatch.items():
        if prim in grouped:
            grouped.update(sibs)

    c = sqlite3.connect(DB)
    # ungrouped SALES that have a pHash
    rows = c.execute("""
        SELECT DISTINCT m.asset_id, m.thumb_hash, m.aspect_ratio, m.r, m.g, m.b
        FROM asset_meta m
        WHERE m.asset_id IN (SELECT DISTINCT asset_id FROM sales)
    """).fetchall()
    ungrouped = [r for r in rows if r[0] not in grouped]
    print(f"sales with pHash: {len(rows)} | ungrouped: {len(ungrouped)}")

    ms = c.execute("SELECT fname, thumb_hash, aspect_ratio, r, g, b FROM ms_meta").fetchall()
    mh = np.array([h2u(r[1]) for r in ms], dtype=np.uint64)
    mar = np.array([r[2] if r[2] is not None else -1 for r in ms], dtype=np.float32)
    mrgb = np.array([[r[3] or 0, r[4] or 0, r[5] or 0] for r in ms], dtype=np.int32)
    mfn = [r[0] for r in ms]
    mgrp = [aliases.get(group_from_fname(f), group_from_fname(f)) for f in mfn]

    confident = ambiguous = nomatch = 0
    samples_ok, samples_amb = [], []
    for aid, h, ar, r, g, b in ungrouped:
        hu = h2u(h)
        ham = popcount(mh ^ np.uint64(hu))
        mask = ham <= HAM_MAX
        if ar is not None:
            mask &= (mar < 0) | (np.abs(mar - ar) <= ASPECT_MAX)
        rgb = np.abs(mrgb - np.array([r or 0, g or 0, b or 0])).sum(1)
        mask &= rgb <= RGB_MAX
        idx = np.where(mask)[0]
        if len(idx) == 0:
            nomatch += 1
            continue
        cand_groups = {mgrp[i] for i in idx if mgrp[i]}
        if len(cand_groups) == 1:
            confident += 1
            if len(samples_ok) < 12:
                gi = idx[np.argmin(ham[idx])]
                samples_ok.append((aid, next(iter(cand_groups)), int(ham[gi]), mfn[gi]))
        else:
            ambiguous += 1
            if len(samples_amb) < 12:
                samples_amb.append((aid, sorted(cand_groups)[:4], int(ham[idx].min())))

    print(f"\n=== DRY-RUN RESULT (ham≤{HAM_MAX}, rgb≤{RGB_MAX}, aspect≤{ASPECT_MAX}) ===")
    print(f"  confident (exactly 1 group): {confident}")
    print(f"  AMBIGUOUS (2+ groups)      : {ambiguous}   ← precision risk")
    print(f"  no match                   : {nomatch}")
    print("\n--- sample confident assignments (asset_id → group, ham) ---")
    for aid, g, hm, fn in samples_ok:
        print(f"  {aid} → {g!r}  ham={hm}  via {fn[:55]}")
    if samples_amb:
        print("\n--- sample AMBIGUOUS (would need a rule) ---")
        for aid, gs, hm in samples_amb:
            print(f"  {aid} → {gs}  minham={hm}")


if __name__ == "__main__":
    main()
