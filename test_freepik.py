"""Mock-transport test for Freepik month-state behaviour (C4/M5)."""
import os, sys, json, tempfile, sqlite3
D = tempfile.mkdtemp(prefix="fp_test_")
os.environ["STOCK_DATA_DIR"] = D
os.makedirs(os.path.join(D, "recipes")); os.makedirs(os.path.join(D, "img_cache"), exist_ok=True)
W = "/Users/admin/Desktop/Stock_Automation/.claude/worktrees/opus-project-review-888be6"
sys.path.insert(0, W)
from db import init_db, DB_NAME
init_db()
import collectors.freepik as f
import sync_state, image_utils
sync_state._sync_log = lambda m: None
f._sync_log = lambda m: None
f.load_img_async = lambda *a, **k: None
f._CSV_FLOOR_YM = "2026-06"
f._last_settled_ym = lambda today=None: "2026-08"

CSV = "Freepik Asset ID,File name,Description,Asset public URL,Freepik Downloads,Freepik Earnings EUR\n" \
      "A1,a1.jpg,desc a,http://x/a,3,1.50\nA2,a2.jpg,desc b,http://x/b,1,0.25\n"
cfg = {"months": {"2026-06": None, "2026-07": CSV, "2026-08": CSV}, "rate": 1.16}
def get(path):
    if path == "/xhr/user": return json.dumps({"id": 42})
    if path.startswith("/xhr/resource/published"): return json.dumps({"data": []})
    if path.startswith("/xhr/stats/download"):
        ym = f"{path.split('year=')[1][:4]}-{path.split('month=')[1][:2]}"
        return cfg["months"].get(ym)
    return None
f._eur_usd_rate = lambda ym, _fetch=None: cfg["rate"]
STATE = os.path.join(D, "recipes", "_freepik_months.json")
def state(): return json.load(open(STATE)) if os.path.exists(STATE) else {}
def rows():
    with sqlite3.connect(DB_NAME) as c:
        return c.execute("SELECT asset_id, price, substr(date,1,10) FROM sales WHERE stock='Freepik' ORDER BY date, asset_id").fetchall()

# S1: June transport failure → not recorded; July/Aug collected; result "transient"
assert f._freepik_run(get) == "transient"
st = state()
assert "2026-06" not in st and "2026-07" in st and "2026-08" in st, st
assert len(rows()) == 4, rows()
assert abs(rows()[0][1] - 1.5 * 1.16) < 1e-6
print("S1 ok", st)

# S2: HTML login page with 200 for June → still not recorded
cfg["months"]["2026-06"] = "<html><body>login</body></html>"
assert f._freepik_run(get) == "transient"
assert "2026-06" not in state()
print("S2 ok (html not treated as empty month)")

# S3: FX unavailable for June → skipped, not recorded
cfg["months"]["2026-06"] = CSV; cfg["rate"] = None
assert f._freepik_run(get) == "transient"
assert "2026-06" not in state() and len(rows()) == 4
print("S3 ok (no rate → month skipped)")

# S4: legacy month stored at fallback 1.08 → re-collected with real rate, rows replaced
st = state(); st["2026-07"]["rate"] = 1.08; json.dump(st, open(STATE, "w"))
with sqlite3.connect(DB_NAME) as c:
    c.execute("UPDATE sales SET price=price/1.16*1.08 WHERE stock='Freepik' AND substr(date,1,7)='2026-07'")
cfg["rate"] = 1.16
assert f._freepik_run(get) is True
r = rows(); st = state()
assert st["2026-07"]["rate"] == 1.16, st
july = [x for x in r if x[2].startswith("2026-07")]
assert len(july) == 2 and abs(july[0][1] - 1.5 * 1.16) < 1e-6, july
assert "2026-06" in st and len(r) == 6, (st, r)
print("S4 ok (fallback-rate month re-collected)", july)
print("ALL FREEPIK TESTS OK")
