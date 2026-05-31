# Windows Port — Session Handoff Notes

Context: app was developed on macOS; this session ported it to run on Windows
(user moved from MacBook to a Windows 11 PC, data dir copied over). Repo:
`tarabamba-maker/nimbus-pkg`, cloned locally at `C:\Users\vkova\nimbus-pkg`.

Runtime data dir on this Windows PC: `%APPDATA%\StockAutomation`
(`C:\Users\vkova\AppData\Roaming\StockAutomation`) — this is `STOCK_DATA_DIR`
(set by Tauri lib.rs `data_dir()`), holds `sales.db`, `recipes/`, `chrome_profile*`,
`getty_profile`, `img_cache*`.
Installed (bundled) copy of the backend that runs live:
`%LOCALAPPDATA%\Stock Automation\_up_\_up_\main.py` — kept in sync with the repo
`main.py` during this session (edit repo → copy to bundled, or vice-versa).

---

## STATUS: all changes are in the working tree, NOT yet committed/pushed.

`git -C C:\Users\vkova\nimbus-pkg status`:
- M main.py
- M ui-tauri/src-tauri/src/lib.rs
- M .github/workflows/build-windows.yml
- M .gitignore
- ?? ui-tauri/src-tauri/tauri.windows.conf.json

Next session: review diff, commit, push. (User wanted everything finished +
these notes before pushing.)

---

## What was done & verified live on this PC

### 1. Environment (manual, one-time on THIS machine — the installer will do it for new PCs once built, see §6)
- Installed Python 3.12.10 (winget) at `%LOCALAPPDATA%\Programs\Python\Python312`.
- `pip install flask flask-cors playwright pillow beautifulsoup4 requests browser-cookie3 comtypes`.
  ⚠️ `flask-cors` was the missing piece — lib.rs `python_has_deps` imports it, so
  without it the launcher reports "python3 not found" even with everything else.
- `playwright install chromium` (NOT needed going forward — see §3, Windows uses system Chrome).
- Created `python3.exe` = copy of `python.exe` in the Python dir (python.org installer
  only ships `python.exe`; the app/launcher also probes `python3`).

### 2. Backend startup crash (FIXED) — main.py top (~line 8)
Windows console is cp1252; the app's emoji `print()` (🚀✅⚠️…) raised
`UnicodeEncodeError` and killed the backend at startup. Fix: force stdout/stderr to
UTF-8 on Windows right after `IS_WIN` is defined.

### 3. Cookie import on Windows — the original bug (FIXED)
Root cause chain:
- No Python → backend never started (fixed in §1).
- Chrome 127+ **App-Bound Encryption (v20)**: the user's native Chrome cookies are
  `v20`, undecryptable outside Chrome itself (the `IElevator` elevation-service
  COM call is path-validated and rejects non-Chrome callers — proven dead end; do
  NOT pursue process injection).
- Therefore: abandoned reading native Chrome cookies. New approach (**variant 3**):
  the app drives **real system Chrome** (Playwright `channel='chrome'`, automation
  flags stripped → `navigator.webdriver=false`) on its OWN profile; user logs in;
  cookies live in the app profile as plain **v10** (automation-launched Chrome keeps
  ABE off) and decrypt directly.

main.py changes:
- `_open_browser_context`: on Windows ALWAYS `channel='chrome'` (system Chrome), and
  drop the hardcoded macOS user-agent on non-Mac (it mismatched Win platform → DataDome flag).
- `_apply_stealth`: **no-op on non-Mac.** `_STEALTH_JS` spoofs `navigator.platform=MacIntel`
  + Apple WebGL to match the Mac UA on bundled Chromium; on real Windows Chrome those
  spoofs create a platform mismatch that DataDome blocks instantly. THIS was why
  Shutterstock login was blocked. macOS still applies stealth.
- New `_dpapi_unprotect` (ctypes) + `_decrypt_chrome_cookie_db(cookie_db)`:
  reads a Chrome cookie SQLite, decrypts v10/v11 (AES-256-GCM under DPAPI-wrapped key,
  strips the 32-byte SHA256(domain) prefix Chrome 124+ prepends), skips v20.
- `_load_appprofile_cookies_windows()`: aggregates cookies from all app profiles
  (`chrome_profile*`, `*_profile`). `_load_browser_cookies()` on Windows uses this
  instead of native Chrome.
- `_windows_browser_login(stocks)` + `_STOCK_LOGIN_URLS`: opens ONE real Chrome window
  with one tab per stock on the seed profile `chrome_profile`; user logs into all,
  closes window; cookies persist + are read back by the aggregator. Shutterstock-specific:
  clears its (possibly DataDome-poisoned) cookies first so it issues a fresh token;
  tabs are staggered (Shutterstock first) to avoid a request burst.
- `/api/import-chrome-cookies` on Windows runs `_windows_browser_login` instead of the
  old (broken on v20) native-cookie copy.

Verified: after manual Shutterstock login, profile yields `datadome` + `accts_contributor`;
5-tab flow captured all 5 stocks.

### 4. Getty/iStock — was returning $0 (FIXED) — `_getty_collect_direct` (~line 1700)
The TSV statement export used a **hardcoded contract id `8368474:True`** (the original
dev's contract) as a fallback because the HTML regex never matched. With the wrong
contract, every statement export came back header-only → "+0 нових". Real fix: read the
active contract from `AvailableStatementPeriod` JSON → `Options.AvailableContracts`
(pick `Selected==true`, else first with a Value). This user's contract is `8301222:True`.
Result: **+21962 iStock sales** imported. Also: empty exports no longer marked "done"
(so a fixed contract retries them).

### 5. Login UX (DONE) — single-stock vs Sync All
`_collect_one_stock_global(name, url, allow_login=False)` and
`_run_collector_global(..., allow_login=False)` now take a flag:
- A **single-stock button** (`/api/sync/start {stock}` → `_run_single`) passes
  `allow_login=True` → opens that stock's login window if the session is missing/expired.
- **Sync All** passes `allow_login=False` → when a stock needs login it just logs
  "🔒 потрібен логін — натисни кнопку «X»" and skips it (no unwanted login windows for
  stocks the user doesn't care about, e.g. Depositphotos).
NOTE: the per-stock buttons already exist in `ui-tauri/src/lib/tabs/Browser.svelte`
(STOCKS row → `startStream(s)`); no frontend change was needed. NOT yet live-tested in
the app (couldn't log a stock out non-destructively) — verify next session.

### 6. Self-install on a fresh PC (IMPLEMENTED, needs CI + fresh-PC verification)
Approach A — bundle an embeddable CPython with deps as a Tauri resource; no Playwright
Chromium (Windows uses system Chrome, a hard requirement anyway).
- `ui-tauri/src-tauri/tauri.windows.conf.json` (NEW): adds `../../pyembed` to
  `bundle.resources` for Windows builds only (so the Mac build is untouched).
- `.github/workflows/build-windows.yml`: new "Bundle embeddable Python" step before
  `tauri build` — downloads python-3.12.10-embed-amd64, enables site-packages in the
  `._pth`, bootstraps pip via get-pip.py, installs the runtime deps into it, copies
  `python3.exe`, sanity-checks imports.
- `lib.rs find_python3`: step 0 checks `<resource>/_up_/_up_/pyembed/python.exe` FIRST.
- `_ensure_playwright_browsers()`: on Windows verifies system Chrome launches
  (`channel='chrome'`) and NEVER downloads Chromium.
- VERIFIED LOCALLY (not full installer): ran the exact CI bundle steps in a temp dir —
  embeddable Python imported all deps AND launched Playwright via system Chrome OK.
- ⚠️ NOT verified: the Tauri resource packaging + lib.rs resolution on a real fresh PC.
  To verify: cut a release (Mac `release.sh` publishes → `build-windows.yml` runs on
  `release: published`) → install the NSIS `*-setup.exe` on a clean Windows VM/PC with
  Google Chrome but no Python.
- `.gitignore`: ignores `pyembed/`, `py-embed.zip`, `get-pip.py`.

### 7. Adobe thumbnails had watermark (FIXED for new syncs) — `_adobe_api_collect_global` (~lines 702, 809)
Display thumb stored the raw watermarked `thumbnailUrl`. Now wrapped with
`_adobe_clean_thumb_url(...)` → clean 110px preview. Only affects NEW/re-cached records;
existing `img_cache/` entries stay watermarked until re-fetched (clear `img_cache/` or
re-sync to refresh).

### 8. Getty cross-stock matching (RESOLVED — was stale state, not a code bug)
Getty/iStock sales use the **MasterId** (e.g. 1796995378) from the TSV (needed for prices);
`ms_library.json` stores Getty **ESP creative-ids** (e.g. 388496214) — different ID
namespaces, so ID-based matching can't link them. They match **visually (pHash)** instead.
The iStock sale thumbnails ARE downloaded + pHashed (2088/2128 unique in `asset_meta`).
Matching appeared broken because `recipes/_matches_state.json` was **carried over from
the Mac** — its `last_asset_meta_rowid` happened to equal the Windows value → false
"cached, nothing changed" early-exit. Fix: deleted `_matches_state.json` +
`_ms_group_snapshot.json` and forced a full rebuild → `hash_pairs:2174`,
`ms_lib_pairs:6797`, auto_grouped 10985. Matching now works like Mac.
Hardening added: `api_rebuild_matches` now drops a saved rowid cursor that is **greater
than** the current table max (foreign/stale state guard).

---

## Pending / next session
1. **Commit + push** all changes (user gate: fresh-PC installability — see §6).
2. **Cut a release and test the Windows installer on a clean PC/VM** (§6) — the only
   way to truly verify self-install. Watch for embeddable-Python `._pth`/pip quirks and
   Tauri resource path resolution in `lib.rs`.
3. Live-test the §5 login UX (Sync All should log, not pop windows; a stock button should
   open just that stock's login).
4. Optional: clear `img_cache/` so existing Adobe thumbs refresh without watermark (§7).
5. Optional polish: auto-invalidate the rebuild cache after a sync that adds records, so
   the user never has to think about Rebuild (the §8 guard covers the cross-machine case
   but not "new data, same machine, incremental missed a pair").

## Key gotchas learned
- lib.rs `python_has_deps` import list MUST match main.py top imports (incl. `flask_cors`,
  `browser_cookie3`). Out of sync → "python3 not found".
- Windows `python`/`python3` resolve to the Microsoft Store stub in `WindowsApps` unless a
  real Python dir precedes it on PATH; python.org installer ships no `python3.exe`.
- Automation-launched Chrome (even real system Chrome via Playwright) keeps App-Bound
  Encryption OFF → app-profile cookies are plain v10.
- Force-killing Chrome loses unflushed cookies; close via window close / graceful exit.
- Chrome 136+ blocks DevTools/remote-debugging on the DEFAULT user-data-dir → can't attach
  Playwright/CDP to the user's everyday Chrome profile.
- DataDome blocks bundled Chromium and blocks real Chrome if the fingerprint is inconsistent
  (Mac stealth spoofs on Windows). Real Chrome + no stealth + consistent UA = passes.
- `PowerShell` here is Windows PowerShell 5.1: no `&&`, emoji in piped Python one-liners can
  hit cp1252 encode errors in the console.
