"""
test_api.py — Quick smoke test for all Flask API endpoints.
Run: python3 test_api.py
Expects Flask to be running on localhost:8000.
"""

import json
import sys
import requests

BASE = "http://localhost:8000"
PASS = 0
FAIL = 0

def check(method, path, expected_type=None, body=None, expect_status=200, timeout=15):
    global PASS, FAIL
    url = BASE + path
    try:
        if method == 'GET':
            r = requests.get(url, timeout=timeout)
        else:
            r = requests.post(url, json=body or {}, timeout=timeout)

        if r.status_code != expect_status:
            print(f"  ✗ {method} {path} → HTTP {r.status_code} (expected {expect_status})")
            FAIL += 1
            return None

        data = r.json()

        if expected_type and not isinstance(data, expected_type):
            print(f"  ✗ {method} {path} → wrong type: {type(data).__name__} (expected {expected_type.__name__})")
            FAIL += 1
            return None

        print(f"  ✓ {method} {path}")
        PASS += 1
        return data
    except Exception as e:
        print(f"  ✗ {method} {path} → {e}")
        FAIL += 1
        return None

def check_img(path):
    global PASS, FAIL
    url = BASE + path
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            print(f"  ✗ GET {path} → HTTP {r.status_code}")
            FAIL += 1
            return
        if not r.content[:2] in (b'\xff\xd8', b'\x89P'):  # JPEG or PNG
            print(f"  ✗ GET {path} → not an image ({r.content[:4]})")
            FAIL += 1
            return
        print(f"  ✓ GET {path}")
        PASS += 1
    except Exception as e:
        print(f"  ✗ GET {path} → {e}")
        FAIL += 1

print("\n=== Stock Automation API smoke test ===\n")

print("── Feed ─────────────────────────────")
check('GET',  '/api/stock-list',   list)
check('GET',  '/api/stats',        dict)
check('GET',  '/api/feed',         dict)
check('GET',  '/api/sales',        dict)
check('GET',  '/api/stock-colors', dict)

print("\n── Groups ───────────────────────────")
groups = check('GET', '/api/groups?preview=2', list)
if groups:
    n = len(groups)
    print(f"     ({n} groups, top: {groups[0]['name'] if groups else '-'})")
check('GET', '/api/photo-groups',  dict)
check('GET', '/api/group-names',   list)
if groups:
    first_name = groups[0]['name']
    check('GET', f'/api/group-photos?name={requests.utils.quote(first_name)}', dict)

print("\n── Matching ─────────────────────────")
matches = check('GET', '/api/matches', dict)
if matches:
    print(f"     ({len(matches)} match entries)")
check('POST', '/api/rebuild-matches', dict, timeout=120)

print("\n── Sync ─────────────────────────────")
check('GET', '/api/sync/status',       dict)
check('GET', '/api/sync/recent-keys',  list)
check('GET', '/api/inspector/status',  dict)

print("\n── Images ───────────────────────────")
check_img('/img/placeholder')
# Try first asset_id from feed
feed = requests.get(BASE + '/api/feed').json()
if feed.get('items'):
    aid = feed['items'][0]['asset_id']
    check_img(f'/img/cache/{aid}')

print("\n── Analytics ────────────────────────")
check('GET', '/api/analytics', dict) if False else print("  (skipped — not in blueprint yet)")

print(f"\n{'='*40}")
print(f"  PASSED: {PASS}   FAILED: {FAIL}")
print(f"{'='*40}\n")
sys.exit(0 if FAIL == 0 else 1)
