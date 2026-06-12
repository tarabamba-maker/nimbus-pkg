"""
routes/feed.py — Sales feed and stats endpoints.

Moved verbatim from main.py. No logic changes.
Routes: /update, /api/sales, /api/feed, /api/stats, /api/stock-list, /api/stock-colors
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from app_globals import DB_NAME, RECIPES_DIR, _load_matches
from sync_state import _save_record, _session_new_keys, _session_new_keys_lock

feed_bp = Blueprint('feed', __name__)

_DEFAULT_STOCK_COLORS = {
    'Adobe Stock':    '#f97316',
    'Shutterstock':   '#e11d48',
    'Getty Images':   '#a855f7',
    'iStock':         '#6366f1',
    'iStockphoto':    '#6366f1',
    'Depositphotos':  '#0061ff',
    'Envato':         '#81b441',
}
_STOCK_COLORS_FILE = os.path.join(RECIPES_DIR, 'stock_colors.json')


def _load_stock_colors():
    try:
        with open(_STOCK_COLORS_FILE) as f:
            saved = json.load(f)
        return {**_DEFAULT_STOCK_COLORS, **saved}
    except Exception:
        return dict(_DEFAULT_STOCK_COLORS)


def _save_stock_colors(colors):
    with open(_STOCK_COLORS_FILE, 'w') as f:
        json.dump(colors, f, indent=2)


@feed_bp.route('/update', methods=['POST'])
def api_update():
    d = request.json
    if not d:
        return jsonify({"status": "error"}), 400
    _save_record(d)
    return jsonify({"status": "success"}), 200


@feed_bp.route('/api/sales', methods=['GET'])
def api_sales():
    """Повертає агреговані продажі для Best Sellers tab."""
    period   = request.args.get('period', 'All-time')
    stock    = request.args.get('stock', 'All')
    page_n   = int(request.args.get('page', 1))
    per_pg   = int(request.args.get('per_page', 50))
    q        = request.args.get('q', '').strip()
    sort_by  = request.args.get('sort', 'total')   # 'total' | 'count'
    sort_dir = request.args.get('dir', 'desc')      # 'desc' | 'asc'

    now = datetime.now()
    cutoffs = {
        'Today': now.strftime('%Y-%m-%d'),
        'Week':  (now - timedelta(days=7)).strftime('%Y-%m-%d'),
        'Month': (now - timedelta(days=30)).strftime('%Y-%m-%d'),
        'Year':  (now - timedelta(days=365)).strftime('%Y-%m-%d'),
    }

    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        params = []
        where  = []
        if period in cutoffs:
            where.append("date >= ?"); params.append(cutoffs[period])
        if stock != 'All':
            where.append("stock = ?"); params.append(stock)
        if q:
            where.append("CAST(asset_id AS TEXT) LIKE ?"); params.append(f"%{q}%")
        ws = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT asset_id, stock, SUM(price), COUNT(*), MAX(thumb_url) "
            f"FROM sales {ws} GROUP BY asset_id, stock", params).fetchall()

    matches = _load_matches()
    aid_to_primary: dict = {}
    for primary, members in matches.items():
        for m in members:
            aid_to_primary[m] = primary

    agg = {}
    for asset_id, sk, row_total, row_count, thumb in rows:
        aid = str(asset_id)
        pr  = float(row_total or 0)
        key = aid_to_primary.get(aid, aid)
        if key in agg:
            agg[key]['total']   += pr
            agg[key]['count']   += row_count
            if not agg[key]['thumb_url'] and thumb: agg[key]['thumb_url'] = thumb
            agg[key]['by_stock'].setdefault(sk, {'total': 0.0, 'count': 0})
            agg[key]['by_stock'][sk]['total'] += pr
            agg[key]['by_stock'][sk]['count'] += row_count
        else:
            agg[key] = {'asset_id': key, 'total': pr, 'count': row_count,
                        'thumb_url': thumb or '', 'stock': sk,
                        'merged': False,
                        'by_stock': {sk: {'total': pr, 'count': row_count}}}

    for v in agg.values():
        v['merged'] = len(v['by_stock']) > 1

    sort_key  = 'count' if sort_by == 'count' else 'total'
    all_items = sorted(agg.values(), key=lambda x: x[sort_key], reverse=(sort_dir != 'asc'))
    total_sum   = sum(v['total'] for v in agg.values())
    total_count = len(all_items)
    start = (page_n - 1) * per_pg
    page_items = all_items[start:start + per_pg]

    return jsonify({
        "total_sum":   round(total_sum, 2),
        "total_count": total_count,
        "page":        page_n,
        "per_page":    per_pg,
        "items":       page_items,
    })


@feed_bp.route('/api/feed', methods=['GET'])
def api_feed():
    """Повертає стрічку продажів для Downloads tab."""
    period = request.args.get('period', 'All-time')
    stock  = request.args.get('stock', 'All')
    page_n = int(request.args.get('page', 1))
    per_pg = int(request.args.get('per_page', 50))

    now = datetime.now()
    cutoffs = {
        'Today': now.strftime('%Y-%m-%d'),
        'Week':  (now - timedelta(days=7)).strftime('%Y-%m-%d'),
        'Month': (now - timedelta(days=30)).strftime('%Y-%m-%d'),
        'Year':  (now - timedelta(days=365)).strftime('%Y-%m-%d'),
    }

    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        params = []
        where  = []
        if period in cutoffs:
            where.append("date >= ?"); params.append(cutoffs[period])
        if stock != 'All':
            where.append("stock = ?"); params.append(stock)
        ws = ("WHERE " + " AND ".join(where)) if where else ""
        total_count = conn.execute(f"SELECT COUNT(*) FROM sales {ws}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, asset_id, price, thumb_url, date, stock "
            f"FROM sales {ws} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [per_pg, (page_n - 1) * per_pg]).fetchall()

    aid_set = list(dict.fromkeys(str(r[1]) for r in rows))
    by_stock_map = {}
    if aid_set:
        matches = _load_matches()
        rev_match: dict = {}
        for primary, members in matches.items():
            for m in members:
                rev_match[str(m)] = str(primary)

        expanded: set = set(aid_set)
        for aid in aid_set:
            primary = rev_match.get(aid, aid)
            group = matches.get(primary) or matches.get(aid)
            if group:
                expanded.update(str(m) for m in group)

        with sqlite3.connect(DB_NAME, timeout=15) as conn2:
            exp_list = list(expanded)
            ph2 = ','.join('?' * len(exp_list))
            agg = conn2.execute(
                f"SELECT asset_id, stock, SUM(price), COUNT(*), MIN(date) FROM sales"
                f" WHERE asset_id IN ({ph2})"
                f" GROUP BY asset_id, stock", exp_list).fetchall()

        raw_by_stock: dict = {}
        for r in agg:
            a = str(r[0])
            raw_by_stock.setdefault(a, {})[r[1]] = {
                "total": round(float(r[2] or 0), 2), "count": r[3] or 0}

        for aid in aid_set:
            primary = rev_match.get(aid, aid)
            group = matches.get(primary) or matches.get(aid) or [aid]
            merged: dict = {}
            for m in group:
                m = str(m)
                for sk, sv in (raw_by_stock.get(m) or {}).items():
                    merged.setdefault(sk, {'total': 0.0, 'count': 0})
                    merged[sk]['total'] = round(merged[sk]['total'] + sv['total'], 2)
                    merged[sk]['count'] += sv['count']
            by_stock_map[aid] = merged

    items = [{'id': r[0], 'asset_id': str(r[1]), 'price': float(r[2] or 0),
              'thumb_url': r[3] or '', 'date': (r[4] or '')[:10],
              'stock': r[5] or '',
              'by_stock': by_stock_map.get(str(r[1]), {})} for r in rows]
    return jsonify({"total_count": total_count, "page": page_n,
                    "per_page": per_pg, "items": items})


@feed_bp.route('/api/stats', methods=['GET'])
def api_stats():
    """Загальна статистика з delta. ?stock=All|Adobe Stock|Shutterstock|iStock"""
    now = datetime.now()
    stock_filter = request.args.get('stock', 'All')

    if stock_filter and stock_filter != 'All':
        base_where  = "stock = ?"
        base_params = [stock_filter]
    else:
        base_where  = "1=1"
        base_params = []

    today_d = now.date()
    week_start       = today_d - timedelta(days=today_d.weekday())
    prev_week_start  = week_start - timedelta(days=7)
    prev_week_end    = today_d - timedelta(days=7)
    month_start      = today_d.replace(day=1)
    _pm_last_day     = (month_start - timedelta(days=1))
    prev_month_start = _pm_last_day.replace(day=1)
    try:
        prev_month_end = prev_month_start.replace(day=today_d.day)
    except ValueError:
        prev_month_end = _pm_last_day
    year_start       = today_d.replace(month=1, day=1)
    prev_year_start  = year_start.replace(year=year_start.year - 1)
    try:
        prev_year_end = today_d.replace(year=today_d.year - 1)
    except ValueError:
        prev_year_end = today_d.replace(year=today_d.year - 1, day=28)
    yesterday = today_d - timedelta(days=1)

    spans = {
        'today': (today_d.strftime('%Y-%m-%d'),
                  yesterday.strftime('%Y-%m-%d'),
                  yesterday.strftime('%Y-%m-%d'), None),
        'week':  (week_start.strftime('%Y-%m-%d'), None,
                  prev_week_start.strftime('%Y-%m-%d'),
                  prev_week_end.strftime('%Y-%m-%d')),
        'month': (month_start.strftime('%Y-%m-%d'), None,
                  prev_month_start.strftime('%Y-%m-%d'),
                  prev_month_end.strftime('%Y-%m-%d')),
        'year':  (year_start.strftime('%Y-%m-%d'), None,
                  prev_year_start.strftime('%Y-%m-%d'),
                  prev_year_end.strftime('%Y-%m-%d')),
    }
    result = {}
    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        def q(extra_where, extra_params):
            r = conn.execute(
                f"SELECT SUM(price), COUNT(*) FROM sales WHERE {base_where} AND {extra_where}",
                base_params + extra_params).fetchone()
            return round(float(r[0] or 0), 2), r[1] or 0

        def q_stock(extra_where, extra_params):
            rows = conn.execute(
                f"SELECT stock, SUM(price), COUNT(*) FROM sales WHERE {base_where} AND {extra_where} GROUP BY stock",
                base_params + extra_params).fetchall()
            return {r[0]: {"total": round(float(r[1] or 0), 2), "count": r[2] or 0} for r in rows}

        for key, (cur_from, cur_to, prev_from, prev_to) in spans.items():
            if cur_to:
                cur_t, cur_c = q("date LIKE ?", [cur_from + '%'])
                prv_t, _     = q("date LIKE ?", [cur_to + '%'])
                ps = q_stock("date LIKE ?", [cur_from + '%'])
            else:
                cur_t, cur_c = q("date >= ?", [cur_from])
                prv_t, _     = q("date >= ? AND date < ?", [prev_from, prev_to])
                ps = q_stock("date >= ?", [cur_from])
            result[key] = {
                "total": cur_t, "count": cur_c,
                "delta": round(cur_t - prv_t, 2),
                "by_stock": ps,
            }

        # Today's green badge = money just added THIS sync dated today (not the
        # partial-today-vs-full-yesterday delta, which is almost always negative
        # and made the day card look unresponsive). Key = id|YYYY-MM-DD|stock|price.
        _today_str = today_d.strftime('%Y-%m-%d')
        with _session_new_keys_lock:
            _keys = list(_session_new_keys)
        _new_today = 0.0
        _new_today_n = 0
        for _k in _keys:
            _parts = _k.split('|')
            if len(_parts) < 4 or _parts[1] != _today_str:
                continue
            if stock_filter and stock_filter != 'All' and _parts[2] != stock_filter:
                continue
            try:
                _new_today += float(_parts[3]); _new_today_n += 1
            except Exception:
                pass
        result['today']['new_total'] = round(_new_today, 2)
        result['today']['new_count'] = _new_today_n

        row = conn.execute(
            f"SELECT SUM(price), COUNT(*) FROM sales WHERE {base_where}", base_params).fetchone()
        result['all'] = {"total": round(float(row[0] or 0), 2), "count": row[1] or 0, "delta": 0}

        by_stock = conn.execute(
            "SELECT stock, SUM(price), COUNT(*) FROM sales GROUP BY stock"
        ).fetchall()
        result['by_stock'] = [{"stock": r[0], "total": round(float(r[1] or 0), 2),
                                "count": r[2]} for r in by_stock]
    return jsonify(result)


@feed_bp.route('/api/stock-list', methods=['GET'])
def api_stock_list():
    """Повертає список стоків."""
    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        rows = conn.execute(
            "SELECT DISTINCT stock FROM sales WHERE stock IS NOT NULL ORDER BY stock"
        ).fetchall()
    stocks = [r[0] for r in rows if r[0]]
    preferred = ["Adobe Stock", "Shutterstock", "iStock"]
    result = [s for s in preferred if s in stocks]
    for s in stocks:
        if s not in result:
            result.append(s)
    if not result:
        result = preferred
    return jsonify(result)


@feed_bp.route('/api/stock-colors', methods=['GET'])
def api_stock_colors_get():
    return jsonify(_load_stock_colors())


@feed_bp.route('/api/stock-colors', methods=['POST'])
def api_stock_colors_set():
    data = request.get_json(force=True, silent=True) or {}
    colors = _load_stock_colors()
    colors.update(data)
    _save_stock_colors(colors)
    return jsonify({'ok': True})
