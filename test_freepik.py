"""
test_freepik.py — ISOLATION test for collectors/freepik.py.
Pure logic only (no live browser): CSV parse, month iteration, settle gate,
FX cache, and a round-trip over the real exported CSV.
Run from repo root: python3 test_freepik.py
"""
import ast
import os
import tempfile
from datetime import date

FAIL = 0
def ok(cond, msg):
    global FAIL
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAIL += 1

print("── syntax ──")
for f in ("collectors/freepik.py", "test_freepik.py"):
    try:
        ast.parse(open(f).read()); ok(True, f"ast.parse {f}")
    except SyntaxError as e:
        ok(False, f"{f}: {e}")

from collectors import freepik as F

print("── _parse_report_csv ──")
sample = (
    "Asset type,File name,Description,Asset public URL,Freepik Asset ID,Freepik Downloads,Freepik Earnings EUR\n"
    "photo,B94A3666.jpg,Some massage,https://www.freepik.com/free-photo/e_14721513.htm,14721513,1,0.0009\n"
    'photo,x.jpg,"Comma, in desc",https://www.freepik.com/free-photo/e_26471081.htm,26471081,82,3.9725\n'
    "photo,,no id row,https://x,,,\n"
)
rows = F._parse_report_csv(sample)
ok(len(rows) == 2, f"parsed 2 rows (skipped empty-id) -> {len(rows)}")
ok(rows[0]["asset_id"] == "14721513" and abs(rows[0]["earnings_eur"] - 0.0009) < 1e-9, "row0 id+eur")
ok(rows[1]["downloads"] == 82 and rows[1]["description"] == "Comma, in desc", "row1 quoted comma + downloads")
ok(F._parse_report_csv("") == [], "empty text -> []")

print("── _month_iter ──")
ms = list(F._month_iter("2024-11", "2025-02"))
ok(ms == ["2024-11", "2024-12", "2025-01", "2025-02"], f"crosses year -> {ms}")
ok(list(F._month_iter("2026-05", "2026-05")) == ["2026-05"], "single month")

print("── _last_settled_ym (10th gate) ──")
ok(F._last_settled_ym(date(2026, 6, 9)) == "2026-04", "before 10th -> 2 months back (Apr)")
ok(F._last_settled_ym(date(2026, 6, 10)) == "2026-05", "on 10th -> previous month (May)")
ok(F._last_settled_ym(date(2026, 1, 5)) == "2025-11", "Jan before 10th wraps year -> 2025-11")
ok(F._last_settled_ym(date(2026, 1, 15)) == "2025-12", "Jan after 10th -> 2025-12")

print("── _invoice_fx_date ──")
ok(F._invoice_fx_date("2024-05") == "2024-06-07", "May -> Jun 7")
ok(F._invoice_fx_date("2024-12") == "2025-01-07", "Dec -> next Jan 7")

print("── _month_date ──")
ok(F._month_date("2024-02") == "2024-02-29", "Feb 2024 leap -> 29")
ok(F._month_date("2024-04") == "2024-04-30", "Apr -> 30")

print("── _eur_usd_rate (injected fetch + cache) ──")
tmp = tempfile.mkdtemp()
F._FX_FILE = os.path.join(tmp, "_fx.json")
calls = {"n": 0}
def fake_fetch(d):
    calls["n"] += 1
    return 1.1234
r1 = F._eur_usd_rate("2024-05", _fetch=fake_fetch)
r2 = F._eur_usd_rate("2024-05", _fetch=fake_fetch)   # cached -> no 2nd call
ok(abs(r1 - 1.1234) < 1e-9, f"rate fetched -> {r1}")
ok(calls["n"] == 1, f"cached on 2nd call (fetch calls={calls['n']})")
ok(os.path.exists(F._FX_FILE), "fx cache written")

print("── real exported CSV round-trip ──")
real = os.path.expanduser("~/Downloads/assets_report_05_2026.csv")
if os.path.exists(real):
    rows = F._parse_report_csv(open(real, encoding="utf-8-sig").read())
    tot = sum(r["earnings_eur"] for r in rows)
    ok(len(rows) == 2236, f"row count -> {len(rows)} (expect 2236)")
    ok(abs(tot - 162.38) < 0.01, f"total EUR -> {tot:.2f} (expect 162.38)")
else:
    print("  (skip: real CSV not in ~/Downloads)")

print("\n" + "=" * 40)
print(f"  {'PASSED' if FAIL == 0 else 'FAILED'}: {FAIL} failure(s)")
print("=" * 40)
raise SystemExit(1 if FAIL else 0)
