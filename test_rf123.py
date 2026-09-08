"""Mock test for 123RF month-state behaviour (C6/H3)."""
import os, sys, json, tempfile, sqlite3
D = tempfile.mkdtemp(prefix="rf_test_")
os.environ["STOCK_DATA_DIR"] = D
os.makedirs(os.path.join(D, "recipes"))
W = "/Users/admin/Desktop/Stock_Automation/.claude/worktrees/opus-project-review-888be6"
sys.path.insert(0, W)
from db import init_db, DB_NAME
init_db()
import collectors.rf123 as rf
import sync_state
sync_state._sync_log = lambda m: None
rf._sync_log = lambda m: None
rf.load_img_async = lambda *a, **k: None
import time; time.sleep = lambda s: None

from datetime import date
cur = date.today().strftime("%Y-%m")
cfg = {"monthly": {"data": [{"data_per_month": [{"simple_date": "2026-06", "total_earnings": "10.00"},
                                                 {"simple_date": "2026-07", "total_earnings": "5.00"}]}]},
       "daily": {"2026-06": {"data": [{"simple_date": "2026-06-05", "total_earnings": "10.00"}]},
                 "2026-07": {"__error": 500}},
       "stats_fail": False}
def get_json(path):
    if path == "/earnings/report/monthly": return cfg["monthly"]
    if path.startswith("/earnings/report/daily"):
        return cfg["daily"].get(path.split("date=")[1], {"data": []})
    if path.startswith("/earnings/report/download_stats"):
        if cfg["stats_fail"]: return {"__error": 502}
        return {"meta": {"pagination": {"total_pages": 1}},
                "data": [{"stockId": "S1", "totalNet": "10.00", "iso8601DateTime": "2026-06-05T03:02:47-04:00",
                          "fileName": "photo.jpg", "thumbnails": {}}]}
    return {"__error": 404}
STATE = os.path.join(D, "recipes", "_123rf_months.json")
def state(): return json.load(open(STATE)) if os.path.exists(STATE) else {}
def rows():
    with sqlite3.connect(DB_NAME) as c:
        return c.execute("SELECT asset_id, price, date, photo_name FROM sales").fetchall()

# S1: June ok, July daily fails → June done, July pending
assert rf._rf123_run(get_json) == "transient"
st = state(); assert "2026-06" in st and "2026-07" not in st, st
r = rows(); assert len(r) == 1 and r[0][2] == "2026-06-05 03:02:47" and r[0][3] == "photo.jpg", r
print("S1 ok", st, r)

# S2: July daily ok but download_stats 502 → July still pending
cfg["daily"]["2026-07"] = {"data": [{"simple_date": "2026-07-01", "total_earnings": "5.00"}]}
cfg["stats_fail"] = True
assert rf._rf123_run(get_json) == "transient"
assert "2026-07" not in state()
print("S2 ok (download_stats failure leaves month pending)")

# S3: daily parsed but zero day rows while total > 0 → pending
cfg["stats_fail"] = False
cfg["daily"]["2026-07"] = {"data": []}
assert rf._rf123_run(get_json) == "transient"
assert "2026-07" not in state()
print("S3 ok (no day rows for non-zero month → pending)")

# S4: all good → July done
cfg["daily"]["2026-07"] = {"data": [{"simple_date": "2026-07-01", "total_earnings": "5.00"}]}
assert rf._rf123_run(get_json) is True
assert "2026-07" in state()
print("S4 ok", state())

# S5: monthly 500 → transient True (not needs_login); 401 → needs_login
cfg["monthly"] = {"__error": 500}
assert rf._rf123_run(get_json) == "transient"
cfg["monthly"] = {"__error": 401}
assert rf._rf123_run(get_json) == "needs_login"
print("S5 ok (500 transient, 401 needs_login)")
print("ALL 123RF TESTS OK")
