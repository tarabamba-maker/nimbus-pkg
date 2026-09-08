"""Mock-transport test for Shutterstock cursor/gap behaviour (C3/H2)."""
import os, sys, json, tempfile, sqlite3
from datetime import date, timedelta
D = tempfile.mkdtemp(prefix="ss_test_")
os.environ["STOCK_DATA_DIR"] = D
os.makedirs(os.path.join(D, "recipes"))
W = "/Users/admin/Desktop/Stock_Automation/.claude/worktrees/opus-project-review-888be6"
sys.path.insert(0, W)
from db import init_db, DB_NAME
init_db()
import collectors.shutterstock as ss
import sync_state
sync_state._sync_log = lambda m: None
ss._sync_log = lambda m: None
ss.time.sleep = lambda s: None
ss._load_browser_cookies = lambda: [
    {"name": "datadome", "value": "x", "domain": ".shutterstock.com"},
    {"name": "accts_contributor", "value": "y", "domain": ".shutterstock.com"},
]
import image_utils
image_utils.load_img_async = lambda *a, **k: None

today = date.today()
PROC = os.path.join(D, "recipes", "_processed_dates.json")
def proc():
    try: return json.load(open(PROC))
    except Exception: return {}

class Resp:
    def __init__(self, code, body=None): self.status_code = code; self._b = body
    def json(self): return self._b

# Scenario config, mutated per scenario
cfg = {"agg_fail_months": set(), "media_403_days": set(), "media_500_days": set()}
def fake_get(url, timeout=None):
    from urllib.parse import urlparse, parse_qs
    u = urlparse(url); q = parse_qs(u.query)
    if "aggregate" in u.path:
        y, m = int(q["year"][0]), int(q["month"][0])
        if (y, m) in cfg["agg_fail_months"]:
            return Resp(500)
        # only the last ~60 days have sales; older months empty (fast walk to floor)
        days = []
        d = date(y, m, 1)
        while d.month == m:
            if today - timedelta(days=60) <= d <= today:
                days.append({"date": d.isoformat(), "on_demand": {"earnings": 1.0}})
            d += timedelta(days=1)
        return Resp(200, {"days": days})
    if "media_stats" in u.path:
        d = q["date"][0]
        if d in cfg["media_403_days"]: return Resp(403)
        if d in cfg["media_500_days"]: return Resp(500)
        return Resp(200, {"pages": 1, "media": [{"mediaId": "M" + d.replace("-", ""), "total": 1.5,
                                                 "details": {"previewImageUrl": "", "description": "t"}}]})
    return Resp(404)

class FakeSession:
    def __init__(self): self.cookies = type("C", (), {"update": lambda self, x: None})(); self.headers = {}
    def get(self, url, timeout=None): return fake_get(url, timeout)
ss.req_lib.Session = FakeSession
ss._sync_stop_flag[0] = False

def count():
    with sqlite3.connect(DB_NAME) as c: return c.execute("SELECT COUNT(*) FROM sales").fetchone()[0]

# ── Scenario 1: aggregate of the previous month fails → recent scan records a gap,
#    backfill does NOT advance the cursor, backfill_done NOT set.
prev_m = (today.replace(day=1) - timedelta(days=1))
cfg["agg_fail_months"] = {(prev_m.year, prev_m.month)}
assert ss._shutterstock_api_collect_direct() is True
p = proc()
assert "Shutterstock_recent_gap_from" in p, p
assert not p.get("Shutterstock_backfill_done"), p
assert "Shutterstock_backfill_cursor" not in p, p
n1 = count(); assert n1 > 0
print("S1 ok: gap=", p["Shutterstock_recent_gap_from"], "rows", n1)

# ── Scenario 2: everything ok → gap cleared, backfill completes to floor.
cfg["agg_fail_months"] = set()
assert ss._shutterstock_api_collect_direct() is True
p = proc()
assert "Shutterstock_recent_gap_from" not in p, p
assert p.get("Shutterstock_backfill_done") is True, p
n2 = count(); assert n2 > n1, (n1, n2)   # the previously failed month's days got collected
print("S2 ok: rows", n2, "backfill done")

# ── Scenario 3: fresh DB, a media day 500s in the backfill → cursor stops at last
#    completed day (bday+1), not past the incomplete one.
import shutil; shutil.rmtree(D); os.makedirs(os.path.join(D, "recipes")); init_db()
bad_day = today - timedelta(days=50)   # inside the 60-day sales window, older than recent scan (45d)
cfg["media_500_days"] = {bad_day.isoformat()}
assert ss._shutterstock_api_collect_direct() is True
p = proc()
assert p.get("Shutterstock_backfill_cursor") == (bad_day + timedelta(days=1)).isoformat(), p
assert not p.get("Shutterstock_backfill_done"), p
print("S3 ok: cursor paused at", p["Shutterstock_backfill_cursor"], "(bad day", bad_day, ")")

# ── Scenario 4: 3× 403 in backfill → cursor = last completed day
cfg["media_500_days"] = set()
cfg["media_403_days"] = {(bad_day - timedelta(days=1)).isoformat()}
# collector resumes at cursor-1 == bad_day; bad_day ok; bad_day-1 → 403 (x3 pages? one page, but counter needs 3)
# force three consecutive 403s on three consecutive days
cfg["media_403_days"] = {(bad_day - timedelta(days=k)).isoformat() for k in (1, 2, 3)}
assert ss._shutterstock_api_collect_direct() is True
p = proc()
# days bad_day-1.. all 403 → each day ok=False → first one already pauses (not blocked yet)
assert p.get("Shutterstock_backfill_cursor") == bad_day.isoformat(), p
print("S4 ok: cursor", p["Shutterstock_backfill_cursor"])
print("ALL SS TESTS OK")
