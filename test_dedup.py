"""Regression test for C1/H4: date-only dedup + no datetime.now() fallback."""
import os, sys, tempfile, sqlite3
D = tempfile.mkdtemp(prefix="dedup_test_")
os.environ["STOCK_DATA_DIR"] = D
sys.path.insert(0, "/Users/admin/Desktop/Stock_Automation/.claude/worktrees/opus-project-review-888be6")
from db import init_db, DB_NAME
import sync_state as ss
init_db()

def rows():
    with sqlite3.connect(DB_NAME) as c:
        return c.execute("SELECT asset_id, stock, price, date FROM sales ORDER BY id").fetchall()

# 1. date-only: same asset, same day, different prices -> 2 rows
ss._save_record({"stock":"Dreamstime","asset_id":"111","price":0.35,"date":"2026-06-01"})
ss._save_record({"stock":"Dreamstime","asset_id":"111","price":2.10,"date":"2026-06-01"})
assert len(rows()) == 2, rows()
# 2. date-only re-sync of same aggregate -> still 2
ss._save_record({"stock":"Dreamstime","asset_id":"111","price":0.35,"date":"2026-06-01"})
assert len(rows()) == 2, rows()
# 3. timestamped: same asset, same day, same price, different times -> both kept
ss._save_record({"stock":"Adobe Stock","asset_id":"A","price":0.99,"date":"2026-05-03T01:13:34+00:00"})
ss._save_record({"stock":"Adobe Stock","asset_id":"A","price":0.99,"date":"2026-05-03T02:00:00+00:00"})
ss._save_record({"stock":"Adobe Stock","asset_id":"A","price":0.99,"date":"2026-05-03T01:13:34+00:00"})
assert len(rows()) == 4, rows()
# 4. app's own 19-char space format is parsed, not stamped today
ss._save_record({"stock":"123RF","asset_id":"R","price":1.0,"date":"2020-01-01 12:00:00"})
assert rows()[-1][3] == "2020-01-01 12:00:00", rows()[-1]
# 5. unparsable date -> dropped, logged
n = len(rows())
ss._save_record({"stock":"X","asset_id":"bad","price":1.0,"date":"yesterday"})
assert len(rows()) == n
assert any("unparsable" in l for l in ss._sync_state["log"]), ss._sync_state["log"]
# 6. m/d/Y date-only, then same via YYYY-MM-DD -> dedup
ss._save_record({"stock":"Depositphotos","asset_id":"D","price":0.5,"date":"06/02/2026"})
ss._save_record({"stock":"Depositphotos","asset_id":"D","price":0.5,"date":"2026-06-02"})
assert len(rows()) == n + 1, rows()
# 7. PIXTA minute-resolution T-form
ss._save_record({"stock":"PIXTA","asset_id":"P","price":0.5,"date":"2026-06-02T10:15:00"})
ss._save_record({"stock":"PIXTA","asset_id":"P","price":0.5,"date":"2026-06-02T10:16:00"})
assert len(rows()) == n + 3, rows()
# 8. new keys recorded only for inserted rows
assert len(ss._session_new_keys) == n + 3, len(ss._session_new_keys)
print("dedup tests OK", rows())
