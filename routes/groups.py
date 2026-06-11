"""
routes/groups.py — Group management endpoints.

Moved verbatim from main.py. No logic changes.
Routes: /api/groups, /api/group-photos, /api/photo-groups/*, /api/groups/match-*,
        /api/photo-groups/merge, /api/ms-library/remove-from-group, /api/group-names
"""

import re
import sqlite3

from flask import Blueprint, jsonify, request

from app_globals import (
    DB_NAME, RECIPES_DIR,
    _RELEVANT_STOCKS, _query_earnings_batch, _load_matches,
)
from image_utils import load_groups, save_groups, load_ms_library, save_ms_library
from sync_state import _app_log

groups_bp = Blueprint('groups', __name__)


@groups_bp.route('/api/groups', methods=['GET'])
def api_groups_get():
    """Groups from ms_library.json + photo_groups.json with earnings from DB.

    Optional query params:
      ?preview=N  — return only the top-N photos per group by earnings (default
                    returns all photos). Use preview=3 for fast list rendering;
                    full photos fetched per-group via /api/group-photos?name=…
    """
    # Period cutoff so the Today/Week/Month/Year blocks filter Groups like Downloads.
    from datetime import datetime as _dt, timedelta as _td
    _now = _dt.now()
    _period = request.args.get('period', 'All-time')
    _cutoff = {
        'Today': _now.strftime('%Y-%m-%d'),
        'Week':  (_now - _td(days=7)).strftime('%Y-%m-%d'),
        'Month': (_now - _td(days=30)).strftime('%Y-%m-%d'),
        'Year':  (_now - _td(days=365)).strftime('%Y-%m-%d'),
    }.get(_period)
    try:
        preview_n = int(request.args.get('preview', 0))
    except ValueError:
        preview_n = 0
    user_groups = load_groups()
    if not user_groups:
        return jsonify([])

    lib = load_ms_library()
    sid_to_photo: dict = {}
    for photo in lib:
        for k, v in (photo.get('stockids') or {}).items():
            if k in _RELEVANT_STOCKS and v:
                sid_to_photo[str(v)] = photo

    ms_groups: dict = {}
    for gname, aids in user_groups.items():
        ms_groups[gname] = []
        seen_keys: set = set()
        for aid in aids:
            photo = sid_to_photo.get(str(aid))
            if photo:
                key = id(photo)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                ms_groups[gname].append({
                    'filename': photo.get('filename', ''),
                    'ms_thumb': photo.get('thumb', ''),
                    'stockids': {k: str(v) for k, v in (photo.get('stockids') or {}).items()
                                 if k in _RELEVANT_STOCKS and v},
                })

    # Cross-stock sibling map: id → set of all cluster members. Lets us fold every
    # matched stock version (incl. stocks NOT in ms_library, e.g. Freepik) into ONE
    # card instead of a separate card per stock.
    _matches = _load_matches()
    sib_of: dict = {}
    for _prim, _members in _matches.items():
        _cluster = {str(_prim)} | {str(m) for m in _members}
        for _x in _cluster:
            sib_of[_x] = _cluster

    all_ids: set = set()
    for photos_raw in ms_groups.values():
        for ph in photos_raw:
            for sid in ph['stockids'].values():
                all_ids.add(str(sid))
                all_ids |= sib_of.get(str(sid), set())
    for aids in user_groups.values():
        all_ids.update(str(a) for a in aids)

    earnings, thumb_map = _query_earnings_batch(all_ids, cutoff=_cutoff)

    result = []

    for gname, photos_raw in ms_groups.items():
        g_total = 0.0; g_sales = 0; g_by_stock: dict = {}
        photos = []
        user_extra = [str(a) for a in user_groups.get(gname, [])]

        for ph in photos_raw:
            stockids = ph['stockids']
            ph_total = 0.0; ph_count = 0; ph_by_stock: dict = {}
            ph_thumb = ''; canonical_id = ''

            # canonical id stays a stable ms_library stockid; earnings fold in the
            # whole cross-stock cluster (so Freepik & co. count on the same card).
            base_sids = [str(s) for s in stockids.values()]
            canonical_id = base_sids[0] if base_sids else ''
            sids = set(base_sids)
            for s in base_sids:
                sids |= sib_of.get(s, set())
            for sid in sids:
                e = earnings.get(sid)
                if not e:
                    continue
                ph_total += e['_total']
                ph_count += e['_count']
                if not ph_thumb and sid in thumb_map:
                    ph_thumb = thumb_map[sid]
                for sk, sv in e.items():
                    if sk.startswith('_'):
                        continue
                    ph_by_stock.setdefault(sk, {'total': 0.0, 'count': 0})
                    ph_by_stock[sk]['total'] += sv['total']
                    ph_by_stock[sk]['count'] += sv['count']

            if not canonical_id:
                continue
            if not ph_thumb:
                ph_thumb = ph.get('ms_thumb', '')
            g_total += ph_total; g_sales += ph_count
            for sk, sv in ph_by_stock.items():
                g_by_stock.setdefault(sk, {'total': 0.0, 'count': 0})
                g_by_stock[sk]['total'] += sv['total']
                g_by_stock[sk]['count'] += sv['count']
            photos.append({
                'asset_id':    canonical_id,
                'filename':    ph.get('filename', ''),
                'thumb':       ph_thumb,
                'earnings':    round(ph_total, 2),
                'sales_count': ph_count,
                'by_stock':    {k: {'total': round(v['total'], 2), 'count': v['count']}
                                for k, v in ph_by_stock.items()},
            })

        represented: set = set()
        for ph_raw in photos_raw:
            for sid in ph_raw['stockids'].values():
                represented.add(str(sid))
                represented |= sib_of.get(str(sid), set())   # collapse sibling stocks
        for p in photos:
            represented.add(p['asset_id'])

        for aid in user_extra:
            if aid in represented:
                continue
            e = earnings.get(aid, {})
            ph_total = e.get('_total', 0.0); ph_count = e.get('_count', 0)
            ph_by_stock = {sk: sv for sk, sv in e.items() if not sk.startswith('_')}
            g_total += ph_total; g_sales += ph_count
            for sk, sv in ph_by_stock.items():
                g_by_stock.setdefault(sk, {'total': 0.0, 'count': 0})
                g_by_stock[sk]['total'] += sv['total']
                g_by_stock[sk]['count'] += sv['count']
            photos.append({
                'asset_id': aid, 'filename': aid,
                'thumb': thumb_map.get(aid, ''),
                'earnings': round(ph_total, 2), 'sales_count': ph_count,
                'by_stock': {k: {'total': round(v['total'], 2), 'count': v['count']}
                             for k, v in ph_by_stock.items()},
            })

        photos.sort(key=lambda x: -x['earnings'])
        result.append({
            'name': gname, 'count': len(photos),
            'total': round(g_total, 2), 'sales': g_sales,
            'by_stock': {k: {'total': round(v['total'], 2), 'count': v['count']}
                         for k, v in g_by_stock.items()},
            'photos': photos,
        })

    ms_names = set(ms_groups.keys())
    for gname, aids in user_groups.items():
        if gname in ms_names or not aids:
            continue
        g_total = 0.0; g_sales = 0; g_by_stock: dict = {}
        photos = []
        for aid in aids:
            aid = str(aid)
            e = earnings.get(aid, {})
            ph_total = e.get('_total', 0.0); ph_count = e.get('_count', 0)
            ph_by_stock = {sk: sv for sk, sv in e.items() if not sk.startswith('_')}
            g_total += ph_total; g_sales += ph_count
            for sk, sv in ph_by_stock.items():
                g_by_stock.setdefault(sk, {'total': 0.0, 'count': 0})
                g_by_stock[sk]['total'] += sv['total']
                g_by_stock[sk]['count'] += sv['count']
            photos.append({
                'asset_id': aid, 'filename': aid,
                'thumb': thumb_map.get(aid, ''),
                'earnings': round(ph_total, 2), 'sales_count': ph_count,
                'by_stock': {k: {'total': round(v['total'], 2), 'count': v['count']}
                             for k, v in ph_by_stock.items()},
            })
        photos.sort(key=lambda x: -x['earnings'])
        result.append({
            'name': gname, 'count': len(photos),
            'total': round(g_total, 2), 'sales': g_sales,
            'by_stock': {k: {'total': round(v['total'], 2), 'count': v['count']}
                         for k, v in g_by_stock.items()},
            'photos': photos,
        })

    represented_groups = set(g['name'] for g in result)
    ms_lib_groups: dict = {}
    for photo in lib:
        gname = photo.get('group', '')
        if gname and gname not in represented_groups:
            ms_lib_groups.setdefault(gname, []).append(photo)

    for gname, photos_raw in ms_lib_groups.items():
        photos = []
        for ph in photos_raw:
            bp = ph.get('basepath', '') or ph.get('filename', '')
            if not bp:
                continue
            photos.append({
                'asset_id':    bp,
                'filename':    ph.get('filename', ''),
                'thumb':       ph.get('thumb', ''),
                'earnings':    0.0,
                'sales_count': 0,
                'by_stock':    {},
            })
        if not photos:
            continue
        result.append({
            'name': gname, 'count': len(photos),
            'total': 0.0, 'sales': 0,
            'by_stock': {},
            'photos': photos,
        })

    result.sort(key=lambda x: x['total'], reverse=True)

    if preview_n > 0:
        for g in result:
            g['photos'] = g['photos'][:preview_n]
    return jsonify(result)


@groups_bp.route('/api/group-photos', methods=['GET'])
def api_group_photos():
    """Return full photos list for a single group — used by the group modal."""
    gname = (request.args.get('name') or '').strip()
    if not gname:
        return jsonify({'error': 'name required'}), 400

    lib = load_ms_library()
    photos_raw = [
        {
            'filename': p.get('filename', ''),
            'ms_thumb': p.get('thumb', ''),
            'stockids': {k: str(v) for k, v in p.get('stockids', {}).items()
                         if k in _RELEVANT_STOCKS and v},
        }
        for p in lib if (p.get('group') or '').strip() == gname
    ]
    user_groups = load_groups()
    user_extra = [str(a) for a in user_groups.get(gname, [])]

    all_ids: set = set()
    for ph in photos_raw:
        all_ids.update(ph['stockids'].values())
    all_ids.update(user_extra)
    earnings, thumb_map = _query_earnings_batch(all_ids)

    photos = []
    represented: set = set()

    for ph in photos_raw:
        stockids = ph['stockids']
        ph_total = 0.0; ph_count = 0; ph_by_stock: dict = {}
        ph_thumb = ''; canonical_id = ''
        for sid in stockids.values():
            represented.add(sid)
            if not canonical_id:
                canonical_id = sid
            e = earnings.get(sid)
            if not e:
                continue
            ph_total += e['_total']
            ph_count += e['_count']
            if not ph_thumb and sid in thumb_map:
                ph_thumb = thumb_map[sid]
            for sk, sv in e.items():
                if sk.startswith('_'):
                    continue
                ph_by_stock.setdefault(sk, {'total': 0.0, 'count': 0})
                ph_by_stock[sk]['total'] += sv['total']
                ph_by_stock[sk]['count'] += sv['count']
        if not canonical_id:
            continue
        if not ph_thumb:
            ph_thumb = ph.get('ms_thumb', '')
        photos.append({
            'asset_id':    canonical_id,
            'filename':    ph.get('filename', ''),
            'thumb':       ph_thumb,
            'earnings':    round(ph_total, 2),
            'sales_count': ph_count,
            'by_stock':    {k: {'total': round(v['total'], 2), 'count': v['count']}
                            for k, v in ph_by_stock.items()},
        })

    for aid in user_extra:
        if aid in represented:
            continue
        e = earnings.get(aid, {})
        ph_total = e.get('_total', 0.0); ph_count = e.get('_count', 0)
        ph_by_stock = {sk: sv for sk, sv in e.items() if not sk.startswith('_')}
        photos.append({
            'asset_id': aid, 'filename': aid,
            'thumb': thumb_map.get(aid, ''),
            'earnings': round(ph_total, 2), 'sales_count': ph_count,
            'by_stock': {k: {'total': round(v['total'], 2), 'count': v['count']}
                         for k, v in ph_by_stock.items()},
        })

    photos.sort(key=lambda x: -x['earnings'])
    return jsonify({'name': gname, 'photos': photos, 'count': len(photos)})


@groups_bp.route('/api/photo-groups', methods=['GET'])
def api_photo_groups_get():
    """Returns photo_groups.json expanded with all pHash cross-stock siblings,
    so group badges appear for Envato/Freepik/iStock/Getty cards in BestSellers/Downloads."""
    result: dict = {}

    for gname, aids in load_groups().items():
        result[gname] = [str(a) for a in aids]

    # Expand to cross-stock siblings so non-primary asset_ids find their group.
    _matches = _load_matches()
    sib_of: dict = {}
    for _prim, _members in _matches.items():
        _cluster = {str(_prim)} | {str(m) for m in _members}
        for _x in _cluster:
            sib_of[_x] = _cluster

    for gname in list(result.keys()):
        expanded = set(result[gname])
        for sid in list(expanded):
            expanded |= sib_of.get(sid, set())
        result[gname] = sorted(expanded)

    return jsonify(result)


@groups_bp.route('/api/photo-groups', methods=['POST'])
def api_photo_groups_post():
    """Оновлює ручні групи.

    ⚠️ Sanitizer (layer-2 guard): GET returns groups EXPANDED to full pHash
    clusters (sib_of), and both UIs persist the whole map back on any edit.
    Without collapsing here, every toggle would write all cross-stock siblings
    INTO photo_groups.json — the exact bloat regression Pass E/F caused.
    Rule: within a group keep ONE id per cluster (first occurrence wins;
    later same-cluster ids are GET-expansion echo-back and get dropped)."""
    data = request.get_json(force=True, silent=True) or {}

    _matches = _load_matches()
    cluster_key: dict = {}
    for _prim, _members in _matches.items():
        for _x in {str(_prim)} | {str(m) for m in _members}:
            cluster_key[_x] = str(_prim)

    # Prefer the id that's ALREADY stored on disk for that group, so the
    # authoritative primary doesn't drift to an arbitrary cluster member.
    disk = load_groups()
    clean: dict = {}
    for gname, aids in (data or {}).items():
        on_disk = {str(a) for a in disk.get(gname, [])}
        by_cluster: dict = {}
        order: list = []
        for a in (aids or []):
            a = str(a)
            ck = cluster_key.get(a, a)   # singleton = its own cluster
            if ck not in by_cluster:
                by_cluster[ck] = a
                order.append(ck)
            elif a in on_disk and by_cluster[ck] not in on_disk:
                by_cluster[ck] = a
        clean[gname] = [by_cluster[ck] for ck in order]

    save_groups(clean)
    return jsonify({"status": "ok"})


@groups_bp.route('/api/photo-groups/<name>', methods=['DELETE'])
def api_photo_groups_delete(name):
    """Видаляє одну групу за назвою."""
    groups = load_groups()
    if name in groups:
        del groups[name]
        save_groups(groups)
    lib = load_ms_library()
    changed = False
    for photo in lib:
        if photo.get('group') == name:
            photo['group'] = ''
            changed = True
    if changed:
        save_ms_library(lib)
    return jsonify({"status": "ok"})


@groups_bp.route('/api/photo-groups/rename', methods=['POST'])
def api_photo_groups_rename():
    """Перейменовує групу: {old_name, new_name}."""
    data = request.get_json(force=True, silent=True) or {}
    old, new = data.get('old_name', ''), data.get('new_name', '').strip()
    if not old or not new:
        return jsonify({"status": "error", "msg": "missing names"}), 400
    groups = load_groups()
    if old not in groups:
        return jsonify({"status": "error", "msg": "group not found"}), 404
    if new in groups:
        return jsonify({"status": "error", "msg": "name taken"}), 409
    groups[new] = groups.pop(old)
    save_groups(groups)
    return jsonify({"status": "ok"})


def _group_base_key(name: str) -> tuple:
    """⚠️ DO NOT TOUCH — group similarity matching rule (per user spec 2026-05-27)."""
    if 'recolor' in name.lower():
        return None
    s = name.strip()
    m = re.match(r'(?:.*?[\\/])?(\d{2}-?\d{2}-?\d{2})\s+(.+?)$', s)
    if not m:
        return None
    date_norm = m.group(1).replace('-', '')
    rest = m.group(2).lower()
    rest = re.sub(r'\s*\((\d+)\)\s*', ' ', rest)
    rest = re.sub(r'\s+p\d+\b', '', rest)
    rest = re.sub(r'\s+cropped\b', '', rest)
    rest = re.sub(r'\s+resize\b', '', rest)
    rest = re.sub(r'\s+colour\b', '', rest)
    rest = re.sub(r'\s+rec\b', '', rest)
    rest = re.sub(r'\s+', ' ', rest).strip()
    if not rest:
        return None
    return (date_norm, tuple(rest.split()))


@groups_bp.route('/api/groups/match-similar', methods=['POST'])
def api_match_similar_groups():
    """⚠️ DO NOT TOUCH — merges variant groups of same shoot into canonical one."""
    from flask import current_app
    groups = load_groups()
    buckets = {}
    for name in list(groups.keys()):
        key = _group_base_key(name)
        if not key: continue
        buckets.setdefault(key, []).append(name)

    merged_pairs = []
    skipped_recolor = 0
    for key, names in buckets.items():
        if len(names) < 2: continue
        names.sort(key=lambda n: (len(n), n))
        canonical = names[0]
        for src in names[1:]:
            try:
                with current_app.test_request_context(
                    json={'source': src, 'target': canonical}, method='POST'):
                    api_photo_groups_merge()
                merged_pairs.append((src, canonical))
            except Exception as ex:
                _app_log(f"[match-similar] merge {src}→{canonical} failed: {ex}")

    return jsonify({'status': 'ok',
                    'merged_count': len(merged_pairs),
                    'pairs': merged_pairs[:50],
                    'recolor_kept_separate': skipped_recolor})


@groups_bp.route('/api/groups/match-within', methods=['POST'])
def api_match_within_group():
    """⚠️ DO NOT TOUCH — pHash matches all sales photos to a single group's visual reference."""
    data = request.get_json(force=True, silent=True) or {}
    gname = (data.get('name') or '').strip()
    if not gname:
        return jsonify({'status': 'error', 'msg': 'missing name'}), 400

    groups = load_groups()
    if gname not in groups:
        return jsonify({'status': 'error', 'msg': 'group not found'}), 404

    member_ids = set(str(a) for a in groups[gname])
    if not member_ids:
        return jsonify({'status': 'ok', 'added': 0})

    all_grouped = set()
    for ids in groups.values():
        all_grouped.update(str(a) for a in ids)

    with sqlite3.connect(DB_NAME, timeout=15) as c:
        placeholders = ",".join(["?"] * len(member_ids))
        member_meta = c.execute(
            f"SELECT asset_id, thumb_hash, aspect_ratio, r, g, b FROM asset_meta "
            f"WHERE asset_id IN ({placeholders}) AND thumb_hash IS NOT NULL",
            list(member_ids)).fetchall()
        all_meta = c.execute(
            "SELECT stock, asset_id, thumb_hash, aspect_ratio, r, g, b FROM asset_meta "
            "WHERE thumb_hash IS NOT NULL").fetchall()

    if not member_meta:
        return jsonify({'status': 'ok', 'added': 0, 'msg': 'no member pHashes'})

    member_ints = [(h, int(h, 16), ar, r, g, b) for h, ar, r, g, b
                   in [(m[1], m[2], m[3], m[4], m[5]) for m in member_meta]]
    added = []
    for stock, aid, ha, ar, r, g, b in all_meta:
        aid_s = str(aid)
        if aid_s in all_grouped: continue
        try: ha_int = int(ha, 16)
        except Exception: continue
        for mh, mh_int, mar, mr, mg, mb in member_ints:
            if ar and mar and abs(ar - mar) > 0.15: continue
            if bin(ha_int ^ mh_int).count('1') > 8: continue
            if (isinstance(r, int) and isinstance(mr, int) and
                abs(r - mr) + abs(g - mg) + abs(b - mb) > 90): continue
            added.append(aid_s); all_grouped.add(aid_s); break

    if added:
        groups[gname] = list(dict.fromkeys(list(groups[gname]) + added))
        save_groups(groups)

    return jsonify({'status': 'ok', 'added': len(added), 'group': gname})


@groups_bp.route('/api/photo-groups/merge', methods=['POST'])
def api_photo_groups_merge():
    """Merges source group into target."""
    data = request.get_json(force=True, silent=True) or {}
    source, target = data.get('source', '').strip(), data.get('target', '').strip()
    if not source or not target:
        return jsonify({'status': 'error', 'msg': 'missing source or target'}), 400
    if source == target:
        return jsonify({'status': 'error', 'msg': 'source == target'}), 400

    src_ids: list = []
    _priority = ['adobestock', 'shutterstock', 'istock', 'esp']
    for photo in load_ms_library():
        if (photo.get('group') or '').strip() != source:
            continue
        stockids = {k: str(v) for k, v in photo.get('stockids', {}).items() if k in _RELEVANT_STOCKS and v}
        canonical = next((stockids[k] for k in _priority if k in stockids), next(iter(stockids.values()), None))
        if canonical and canonical not in src_ids:
            src_ids.append(canonical)

    groups = load_groups()
    for aid in groups.get(source, []):
        if str(aid) not in src_ids:
            src_ids.append(str(aid))

    tgt_ids = [str(a) for a in groups.get(target, [])]
    merged = list(tgt_ids) + [a for a in src_ids if a not in tgt_ids]
    groups[target] = merged
    if source in groups:
        del groups[source]
    save_groups(groups)

    lib = load_ms_library()
    changed = False
    for photo in lib:
        if (photo.get('group') or '').strip() == source:
            photo['group'] = target
            changed = True
    if changed:
        save_ms_library(lib)

    return jsonify({'status': 'ok', 'merged': len(merged)})


@groups_bp.route('/api/ms-library/remove-from-group', methods=['POST'])
def api_ms_remove_from_group():
    """Видаляє фото з ms_library групи: {filename}."""
    data = request.get_json(force=True, silent=True) or {}
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'status': 'error', 'msg': 'missing filename'}), 400
    photos = load_ms_library()
    for ph in photos:
        if ph.get('filename') == filename:
            ph['group'] = ''
            break
    save_ms_library(photos)
    return jsonify({'status': 'ok'})


@groups_bp.route('/api/group-names', methods=['GET'])
def api_group_names():
    """Returns sorted list of all group names (ms_library + user)."""
    lib = load_ms_library()
    ms_names = {p.get('group', '').strip() for p in lib if p.get('group', '').strip()}
    user_names = set(load_groups().keys())
    return jsonify(sorted(ms_names | user_names))
