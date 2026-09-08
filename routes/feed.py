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
    'Freepik':        '#1273eb',
    '123RF':          '#e64a19',
    'PIXTA':          '#00b0a8',
    'Dreamstime':     '#8cc63f',
    'Alamy':          '#00a651',
}
_STOCK_COLORS_FILE = os.path.join(RECIPES_DIR, 'stock_colors.json')

# Thumbnail-quality order (clean → watermarked). For a matched cluster the card
# image should come from the cleanest sibling that actually has a cached thumb.
_THUMB_QUALITY = [
    'Adobe Stock', 'Shutterstock', 'iStock', 'iStockphoto', 'Getty Images',
    'Microstock+', 'Envato', 'Freepik', '123RF', 'Depositphotos',
    'Alamy', 'Dreamstime', 'PIXTA',
]
_THUMB_RANK = {s: i for i, s in enumerate(_THUMB_QUALITY)}


def _thumb_meta_set():
    """asset_ids that have a computed thumbnail (asset_meta row = cached jpg exists)."""
    try:
        with sqlite3.connect(DB_NAME, timeout=15) as c:
            return {str(r[0]) for r in c.execute(
                "SELECT asset_id FROM asset_meta WHERE thumb_hash IS NOT NULL")}
    except Exception:
        return set()


def _best_thumb_aid(stock_aids, meta_set, fallback):
    """Pick the asset_id whose stock has the cleanest thumbnail AND a cached image.
    stock_aids: {stock: asset_id}. Falls back to the card's own asset_id."""
    best = None
    best_rank = 10_000
    for sk, aid in stock_aids.items():
        if aid not in meta_set:
            continue
        rank = _THUMB_RANK.get(sk, 500)
        if rank < best_rank:
            best_rank, best = rank, aid
    return best or fallback


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

    meta_set = _thumb_meta_set()        # asset_ids that actually have a cached thumb

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
        # remember a candidate asset_id per stock for best-thumbnail selection;
        # prefer one that actually has a cached thumb.
        sa = agg[key].setdefault('_stock_aids', {})
        if sk not in sa or (aid in meta_set and sa[sk] not in meta_set):
            sa[sk] = aid

    for v in agg.values():
        v['merged'] = len(v['by_stock']) > 1
        # Matched cards often have a watermarked/low-quality primary thumb — pick the
        # cleanest sibling that has a thumbnail (stock quality priority).
        v['thumb_aid'] = _best_thumb_aid(v.pop('_stock_aids', {}), meta_set, v['asset_id'])

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
        # Ordering: sync batches stack by recency (a later sync's rows always sit
        # ABOVE an earlier sync's — insertion-order semantics that drive the blue
        # new-sale highlights), but WITHIN one batch rows sort by sale date DESC.
        # Parallel collectors interleave inserts in arbitrary order, so plain
        # id DESC mixed dates/stocks inside a batch and made per-stock reading
        # impossible. Pre-feature rows are all batch 0 = one date-sorted history
        # block. This also replaces the old Envato pin hack: its month-end-dated
        # synthetic rows now land at their date position inside their batch.
        rows = conn.execute(
            f"SELECT id, asset_id, price, thumb_url, date, stock "
            f"FROM sales {ws} "
            f"ORDER BY sync_batch DESC, date DESC, id DESC "
            f"LIMIT ? OFFSET ?",
            params + [per_pg, (page_n - 1) * per_pg]).fetchall()

    aid_set = list(dict.fromkeys(str(r[1]) for r in rows))
    by_stock_map = {}
    thumb_aid_map = {}
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

        meta_set = _thumb_meta_set()
        for aid in aid_set:
            primary = rev_match.get(aid, aid)
            group = matches.get(primary) or matches.get(aid) or [aid]
            merged: dict = {}
            stock_aids: dict = {}
            for m in group:
                m = str(m)
                for sk, sv in (raw_by_stock.get(m) or {}).items():
                    merged.setdefault(sk, {'total': 0.0, 'count': 0})
                    merged[sk]['total'] = round(merged[sk]['total'] + sv['total'], 2)
                    merged[sk]['count'] += sv['count']
                    if sk not in stock_aids or (m in meta_set and stock_aids[sk] not in meta_set):
                        stock_aids[sk] = m
            by_stock_map[aid] = merged
            thumb_aid_map[aid] = _best_thumb_aid(stock_aids, meta_set, aid)

    items = [{'id': r[0], 'asset_id': str(r[1]), 'price': float(r[2] or 0),
              'thumb_url': r[3] or '', 'date': (r[4] or '')[:10],
              'stock': r[5] or '',
              'thumb_aid': thumb_aid_map.get(str(r[1]), str(r[1])),
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


@feed_bp.route('/api/analytics', methods=['GET'])
def api_analytics():
    """Aggregated data for the Analytics tab — a timeline (bar/line) + a per-stock
    breakdown (pie/donut).

    Query params:
      period : Today | Week | Month | Year | All-time   (ignored if start+end given)
      stock  : All | <stock name>
      start  : YYYY-MM-DD  (optional custom range, inclusive)
      end    : YYYY-MM-DD  (optional custom range, inclusive)

    Granularity (timeline bucket) is derived from the span:
      <=2d → hour, <=92d → day, <=731d → month, else → year.
    Buckets are zero-filled so the chart line/bars stay continuous.
    """
    period = request.args.get('period', 'All-time')
    stock  = request.args.get('stock', 'All')
    start  = (request.args.get('start') or '').strip()
    end    = (request.args.get('end') or '').strip()

    now = datetime.now()
    today_d = now.date()

    # Resolve [start_d, end_d] (inclusive). Custom range wins over period.
    if start and end:
        try:
            start_d = datetime.strptime(start, '%Y-%m-%d').date()
            end_d   = datetime.strptime(end, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'error': 'bad date'}), 400
        if start_d > end_d:
            start_d, end_d = end_d, start_d
    else:
        end_d = today_d
        if period == 'Today':
            start_d = today_d
        elif period == 'Week':
            start_d = today_d - timedelta(days=6)
        elif period == 'Month':
            start_d = today_d - timedelta(days=29)
        elif period == 'Year':
            start_d = today_d - timedelta(days=364)
        else:  # All-time → from the first sale in the DB
            start_d = None

    where, params = [], []
    if stock != 'All':
        where.append("stock = ?"); params.append(stock)
    if start_d is not None:
        where.append("date >= ?"); params.append(start_d.strftime('%Y-%m-%d'))
    # inclusive upper bound: anything dated on end_d (with or without a time part)
    where.append("date < ?"); params.append((end_d + timedelta(days=1)).strftime('%Y-%m-%d'))
    ws = ("WHERE " + " AND ".join(where)) if where else ""

    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        if start_d is None:
            r = conn.execute(
                f"SELECT substr(MIN(date),1,10) FROM sales {ws}", params).fetchone()
            mn = r[0] if r and r[0] else today_d.strftime('%Y-%m-%d')
            try:
                start_d = datetime.strptime(mn, '%Y-%m-%d').date()
            except ValueError:
                start_d = today_d

        span = (end_d - start_d).days
        if span <= 2:
            gran, slen = 'hour', 13
        elif span <= 92:
            gran, slen = 'day', 10
        elif span <= 731:
            gran, slen = 'month', 7
        else:
            gran, slen = 'year', 4

        rows = conn.execute(
            f"SELECT substr(date,1,{slen}) AS b, stock, SUM(price), COUNT(*) "
            f"FROM sales {ws} GROUP BY b, stock", params).fetchall()
        raw = {}                       # bucket -> {stock: (total, count)}
        for b, sk, tot, cnt in rows:
            raw.setdefault(b, {})[sk] = (round(float(tot or 0), 2), cnt or 0)

        by_stock_rows = conn.execute(
            f"SELECT stock, SUM(price), COUNT(*) FROM sales {ws} GROUP BY stock "
            f"ORDER BY SUM(price) DESC", params).fetchall()

    # ── zero-fill the timeline buckets across the range ──
    buckets = []
    if gran == 'hour':
        cur = datetime(start_d.year, start_d.month, start_d.day)
        last = datetime(end_d.year, end_d.month, end_d.day, 23)
        while cur <= last:
            buckets.append(cur.strftime('%Y-%m-%d %H')); cur += timedelta(hours=1)
    elif gran == 'day':
        cur = start_d
        while cur <= end_d:
            buckets.append(cur.strftime('%Y-%m-%d')); cur += timedelta(days=1)
    elif gran == 'month':
        y, m = start_d.year, start_d.month
        while (y, m) <= (end_d.year, end_d.month):
            buckets.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12: m = 1; y += 1
    else:  # year
        for y in range(start_d.year, end_d.year + 1):
            buckets.append(f"{y:04d}")

    timeline = []
    for b in buckets:
        bs = raw.get(b, {})
        timeline.append({
            'bucket': b,
            'total': round(sum(v[0] for v in bs.values()), 2),
            'count': sum(v[1] for v in bs.values()),
            'by_stock': {sk: {'total': v[0], 'count': v[1]} for sk, v in bs.items()},
        })

    by_stock = [{'stock': r[0], 'total': round(float(r[1] or 0), 2), 'count': r[2] or 0}
                for r in by_stock_rows]

    return jsonify({
        'granularity': gran,
        'start': start_d.strftime('%Y-%m-%d'),
        'end':   end_d.strftime('%Y-%m-%d'),
        'total': round(sum(t['total'] for t in timeline), 2),
        'count': sum(t['count'] for t in timeline),
        'timeline': timeline,
        'by_stock': by_stock,
    })


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


@feed_bp.route('/api/photo-detail', methods=['GET'])
def api_photo_detail():
    """Full CROSS-STOCK breakdown for one photo, regardless of any feed filter.
    Expands the asset over its _cross_stock_matches cluster and aggregates the
    earnings of every sibling id. Used by the photo popup so it always shows
    all stocks even when opened from a stock-filtered tab."""
    aid = str(request.args.get('asset_id', '')).strip()
    if not aid:
        return jsonify({'error': 'asset_id required'}), 400
    matches = _load_matches()
    ids = {aid}
    for primary, members in matches.items():
        if aid == str(primary) or aid in {str(m) for m in members}:
            ids.add(str(primary))
            ids.update(str(m) for m in members)
            break
    id_list = sorted(ids)
    by_stock = {}
    with sqlite3.connect(DB_NAME, timeout=15) as conn:
        for i in range(0, len(id_list), 800):   # SQLite 999-var limit
            chunk = id_list[i:i + 800]
            qm = ','.join('?' * len(chunk))
            for sk, tot, cnt in conn.execute(
                    f"SELECT stock, SUM(price), COUNT(*) FROM sales "
                    f"WHERE asset_id IN ({qm}) GROUP BY stock", chunk):
                d = by_stock.setdefault(sk, {'total': 0.0, 'count': 0})
                d['total'] += float(tot or 0)
                d['count'] += int(cnt or 0)
    return jsonify({'asset_id': aid, 'siblings': id_list, 'by_stock': by_stock})


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
