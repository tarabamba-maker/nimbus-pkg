# Stock Aggregator — CLAUDE.md

## What this is

Desktop sales aggregator for photo stocks. Playwright logs into stock sites, collects sales data, saves to local SQLite DB. Flask — local HTTP server (port 8000). SvelteKit+Tauri — desktop UI.

**Flet has been fully removed.** `main.py` is Flask-only (~5500 lines, modularization in progress). UI is `ui-tauri/` (SvelteKit + Tauri).

**Current version: v0.9.55** (Mac + Windows, GitHub Actions CI builds both).
**Native macOS SwiftUI app: `mac-native/` (see section below) — shares the Python backend.**
**Windows port: COMPLETE** (v0.9.50 merged). Key Windows fixes:
- UTF-8 stdout/stderr (emoji crash fix)
- Chrome ABE v20: uses app-profile cookies via system Chrome (`channel='chrome'`)
- Getty contract ID: reads from `AvailableContracts` API (was hardcoded wrong)
- `_apply_stealth`: no-op on non-Mac (Mac UA spoofs caused DataDome blocks on Windows)
- Bundled embeddable Python in CI (`pyembed/`, `tauri.windows.conf.json`)
- `allow_login` flag: Sync All skips locked stocks; per-stock button opens login

## Stack

| Component | Technology |
|-----------|-----------|
| UI | SvelteKit (Svelte 5 runes) + Tauri 2 |
| Browser | Playwright (persistent Chrome profiles) |
| Backend | Flask, port 8000 |
| DB | SQLite (`sales.db`) |
| Image cache | `img_cache/` (JPEG thumbnails) |

## Files

```
main.py              — Flask app init + blueprint registration only (~284 lines)
utils.py             — Pure utility functions (moved from main.py, Step 1 done):
                        _adobe_clean_thumb_url, _hamming_hex, _dhash_from_path, _dpapi_unprotect
sales.db             — sales database
import_getty.py      — one-shot TSV import for Getty/iStock statements
recipes/             — JSON state files
  _processed_dates.json — already-synced dates between sessions
  photo_groups.json  — user-created photo groups
  _cross_stock_matches.json — primary_id → [sibling_ids] from ms_library
img_cache/           — cached thumbnails (synced from stock APIs)
img_cache_match/     — thumbnails downloaded during MS+ matching
img_cache_ms/        — thumbnails from ms_library.json
chrome_profile/      — persistent Chrome profile (Adobe + Shutterstock default)
chrome_profile_Adobe_Stock/    — dedicated Adobe profile
chrome_profile_Shutterstock/   — dedicated Shutterstock profile
chrome_profile_Depositphotos/  — ready for Depositphotos
chrome_profile_123RF/          — ready for 123RF
chrome_profile_Pond5/          — ready for Pond5
chrome_profile_Envato/         — ready for Envato
chrome_profile_Microstock+/    — Microstock+ (MS+ library matching)
getty_profile/       — ESP Getty/iStock profile (separate from chrome_profile)
grabber_profile/     — inspector/recorder profile
ui-tauri/            — SvelteKit+Tauri desktop app
```

## Architecture: main.py (Flask-only)

### Helpers & utilities

| Function | Purpose |
|----------|---------|
| `_app_log(msg)` | Log to stdout + `app.log` with timestamp |
| `_ensure_playwright_browsers()` | Auto-install Playwright chromium if missing |
| `_adobe_clean_thumb_url(url)` | Normalize Adobe ftcdn.net URL → 110px, no watermark |
| `_load_matches()` / `_save_matches(m)` | R/W `_cross_stock_matches.json` |
| `init_db()` | Create `sales`, `photos`, `photo_assets` tables if missing |
| `is_already_saved(stock, aid, price, date)` | Dedup check before inserting into `sales` |
| `save_to_db(d)` | Insert one sale record into `sales` table |
| `load_all()` | Load all sales from DB (used by legacy queue flushing) |
| `load_groups()` / `save_groups(g)` | R/W `recipes/photo_groups.json` |
| `load_ms_library()` / `save_ms_library(photos)` | R/W `ms_library.json` (mtime-cached) |
| `load_collected_folders()` / `save_collected_folders(s)` | R/W collected folder set (**unused**) |
| `load_merge_history()` / `save_merge_history(h)` | R/W merge history (**unused**) |
| `_query_earnings_batch(ids)` | Batch earnings query in chunks of 800 (SQLite 999-var limit) |
| `_sync_log(msg)` | Append to `_sync_state["log"]` (SSE stream source) |
| `_apply_stealth(ctx)` | Apply stealth JS to Playwright browser context |
| `_is_login_url(url)` | Detect if browser landed on a login page |
| `_open_browser_context(p, profile, headless)` | Open Playwright persistent context |
| `_do_login_flow_global(p, profile, url, label, cond)` | Open browser, wait for manual login if needed |
| `load_img(asset_id, url)` | Download + cache thumbnail to `img_cache/` as 200×200 JPEG |
| `load_match_thumb(asset_id, url)` | Download Adobe thumb to `img_cache_match/` as 200×200 JPEG |
| `load_img_async(asset_id, url, cb, is_adobe)` | Async thumbnail download via ThreadPoolExecutor |
| `pdate(s)` / `pfloat(v)` | Parse date / float — **unused** (legacy parser helpers) |
| `_stock_icon(sk)` | **DEAD CODE** — uses `ft.Image` / `ft.Text` (Flet), never called |

### Flask routes — active (called by Svelte UI)

| Route | Used by |
|-------|---------|
| `GET  /api/feed` | Downloads tab — paginated sales list (`period`, `stock`, `page`, `per_page`) |
| `GET  /api/stats` | Top stat boxes — today/week/month/year totals + delta + by_stock |
| `GET  /api/stock-list` | Stock filter pills in Downloads, BestSellers, Analytics |
| `GET  /api/sales` | BestSellers + Groups (search-to-add) — top photos by revenue |
| `GET  /api/analytics` | Analytics tab — timeline + by_stock breakdown |
| `GET  /api/groups` | Groups tab — full group list with photos + earnings |
| `GET  /api/photo-groups` | +page.svelte — shared photoGroups state |
| `POST /api/photo-groups` | +page.svelte — save user group edits |
| `DEL  /api/photo-groups/<name>` | Groups tab — delete a group |
| `POST /api/photo-groups/rename` | Groups tab — rename a group |
| `POST /api/ms-library/remove-from-group` | Backend-only (Groups.svelte uses `onToggleGroup` instead) |
| `GET  /api/group-names` | **Unused in UI** — returns sorted list of all group names |
| `POST /api/rebuild-matches` | Groups + BestSellers — rebuild cross-stock matches + sync ms_library→photo_groups |
| `GET  /api/matches` | BestSellers + Groups — load `_cross_stock_matches.json` |
| `POST /api/matches` | BestSellers + Groups — save manual match edits |
| `GET  /api/sync/status` | Browser tab — current sync state |
| `GET  /api/sync/stream` | Downloads tab — SSE live sync log |
| `POST /api/sync/start` | Downloads + Browser — start background sync |
| `POST /api/sync/stop` | Browser tab — set stop flag |
| `POST /api/sync/headless` | Browser tab — toggle headless mode |
| `GET  /img/cache/<aid>` | All tabs — serve cached thumbnails |
| `GET  /img/placeholder` | All tabs — 1×1 grey JPEG fallback |
| `GET  /img/ms/<fname>` | Defined in `api.js` but **not used** by any component |

### Flask routes — internal / legacy

| Route | Status |
|-------|--------|
| `POST /update` | Called internally by collectors via `requests.post(...)` — saves one sale |
| `GET  /status` | **Unused** — returns `{"status":"ok"}`, legacy health check |
| `POST /inspect` | **Unused** — was for Flet inspector, saves HTML to `debug/` |

### Collectors

| Function | Description |
|----------|-------------|
| `_adobe_api_collect_global(pw_page)` | Adobe Stock via `/en/insights/sales-earnings` paginated API |
| `_shutterstock_api_collect_global(pw_page)` | Shutterstock via `/api/next/v2/earnings/media_stats/day` (last 92 days) |
| `_getty_api_collect_global(pw_page)` | Getty/iStock via ESP API (`/api/account/v1/statistics/downloads_for_search`) |
| `_run_collector_global(p, profile, stock, url, headless)` | Opens browser + runs one collector by stock name |
| `_collect_one_stock_global(name, url)` | Wrapper: opens Playwright → calls `_run_collector_global` |
| `_sync_all_global()` | Background thread: iterates `STOCK_URLS`, runs all collectors |

### Dead code — fully removed (2026-05-19 session)

The "photos as first-class objects" DB layer (`_photo_id_for`, `_get_photo`, `_get_photo_by_aid`,
`_batch_fetch_photos`, `_upsert_photo`, `migrate_to_photos_db`) was completely removed. `save_to_db`
no longer writes to the dead `photos`/`photo_assets` tables — those tables still exist in `init_db`
but are not read or written by any route.

Also removed: all Flet constants/themes (`ACCENT`/`RED`/`BG`/.../`apply_theme`/`THEMES`),
`CONNECTED_STOCKS`, `load_all()`, `pdate`/`pfloat`, `load_collected_folders` / `save_collected_folders`,
`load_merge_history` / `save_merge_history`, `_stock_icon` (Flet `ft.Image`), `_ASSETS`, `_SK_DISPLAY`,
dead `DEBUG_DIR` / `COLLECTED_FOLDERS_FILE` / `MERGE_HISTORY_FILE` constants, GET `/status` route,
POST `/inspect` route + `inspect_queue`, unused `queue` global, unused `sys` / `queue` imports.

## Tauri backend lifecycle (lib.rs)

Tauri auto-starts `main.py` on launch and kills it on exit:
- `start_backend()`: `pkill -f main.py`, wait 500ms, `python3 main.py`
- `stop_backend()`: kill child + `pkill -f main.py`
- `RunEvent::Exit` → `stop_backend()`

## macOS native app (`mac-native/`) — SwiftUI, shares the Python backend

Native SwiftUI app (macOS 26 Liquid Glass) alongside the Tauri/Windows UI. Same
Flask backend (`Backend.swift` spawns `main.py` with `STOCK_DATA_DIR` =
`~/Library/Application Support/StockAutomation`, cwd = that dir).

### ⚠️ BUILD + INSTALL PROTOCOL — the #1 time-waster (2026-06-08)
- Build: `cd mac-native && ./build.sh` (plain `swiftc`, NOT SwiftPM — `Package.swift`
  uses `.v26` which the CLI SwiftPM toolchain can't parse). Produces `Stock Mac.app`.
- **The app holds its binary open while running**, so `cp` over a *running*
  `/Applications/Stock Mac.app` SILENTLY FAILS and you keep testing a stale build.
  This cost an entire session: every "it's still broken" was a 13:36 binary while the
  fix was at 14:15. ALWAYS:
  1. `osascript -e 'tell application "Stock Mac" to quit'`; then kill by PID:
     `pgrep -x StockMac | while read p; do kill -9 "$p"; done` (NEVER
     `pkill -f StockMac` / `-f MacOS/StockMac` — the pattern matches your own shell
     command and kills the shell with exit 144, aborting the install mid-way).
  2. `rm -rf "/Applications/Stock Mac.app"; cp -R "Stock Mac.app" /Applications/`
  3. **VERIFY**: `stat -f %Sm` on the installed binary is newer than your edits, and
     `strings … | grep -c SortSegmented` (a known new symbol) > 0. Only then `open`.
- Run each install step as a SEPARATE Bash call; compound `&&` chains abort on the
  exit-144 and leave a half-done install.

### Theme persistence (`SurfaceTheme.swift`)
- `load()` decodes EVERY `Persisted` field as optional and applies it only if present.
  Do NOT make any field non-optional: one missing/older field would throw and reset
  ALL settings (incl. saved positions) to defaults — the "positions reset after save"
  bug. Materials Lab (`MaterialsLab.swift`) is the live editor → `recipes/surface_theme.json`.

### Blue new-sale highlight (recurs every few sessions — READ THIS)
- Backend is the source of truth: `_session_new_keys` (cleared at sync start, appended
  by `_save_record`), read via `GET /api/sync/recent-keys`. `Sale.saleKey` =
  `asset_id|date[:10]|stock|price.2f` matches it exactly.
- The bug is ALWAYS the same: a sync path that reloads the feed but never fetches the
  keys. EVERY sync completion MUST call `model.notifySyncDone()` (fetches keys, clears
  the tab cache, refreshes) BEFORE the feed reload. Downloads `doSync()` and
  BrowserView both do this now. Don't add a sync path that skips it.

### Period blocks — same logic on every tab
- The Today/Week/Month/Year cards set `model.period`. Every tab reacts with
  `.task(id: model.period)`. Best Sellers passes period to `/api/sales`; Groups passes
  it to `/api/groups` (backend: `_query_earnings_batch(ids, cutoff=)` + period→cutoff
  in `api_groups_get`). Downloads uses `/api/feed`.

### Pills (`Pills.swift`) — single source of truth
- `SegmentedGroup` (filters/tabs) + `SortSegmented` (Earnings/Sales/Name sort, with ↑/↓
  arrow, double-click flips direction). Standalone buttons use `PillButtonStyle`.
- SurfaceTheme drives: `pillColor`, `pillRadius`, `pillOffsetX/Y`,
  `pillTextColor(isDark:)` (per-theme: white in dark, BLACK in light by default).
- Light theme: inactive pills/buttons use `glass .regular` tint 0.2 (not the dark fill).

### Animations in use (2026-06-08)
- `.contentTransition(.numericText())` on all `$`/count totals (stat blocks + cards).
- `.scrollTransition` fade+scale on every card as it enters.
- One-shot `.scaleEffect` pulse on new (blue) sale cards via `onAppear`.
- Sort arrow `.contentTransition(.symbolEffect(.replace))`.
- Segmented selection morph = `matchedGeometryEffect(id:"segsel")`.
- NOT done (need a focused visual pass, don't attempt blind): card→`PhotoPopup` zoom
  (popups are overlay-based `popupHost`, not NavigationStack so `.navigationTransition(.zoom)`
  doesn't apply); deeper `glassEffectID` glass-morph refactor of the pills.

## Stocks — status

### ✅ Adobe Stock
- **Method: API** (`_adobe_api_collect_global` in main.py:559+)
- Required header: `x-requested-with: XMLHttpRequest` — without it server returns HTML not JSON
- **Endpoint:** `GET /en/insights/sales-earnings`
- Pagination: `view.pagination` → `{limit, page, pages, total}` — iterate 1..pages
- Data in `sales.history[]` (NOT `body.history` — always empty!) → `{id, commissionAmount, thumbnailUrl, saleDate, title, originalName}`
- `saleDate` format: `"2026-05-03T01:13:34+00:00"` → slice `[:10]` = `"2026-05-03"`
- Dedup: `is_already_saved()` per record (not per month)

#### Two-pass strategy — DO NOT TOUCH unless Adobe API changes
1. **Pass 1 (recent, no date filter):** `?limit=1000&page=N&pv={ms}` — pulls most recent ~5000 sales. Has early-stop: if a page is 100% duplicates → `break` (NOT `return` — that would skip Pass 2!). 
2. **Pass 2 (historical chunks):** `?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&time_range=day&pv={ms}&limit=1000&page=N` — walks 10 years back in **360-day chunks** (Adobe max is 364 — keep ≥4 day margin).

#### Critical state file: `recipes/_processed_dates.json` → `"Adobe Stock": ["YYYY-MM", ...]`
- A chunk's months are marked done **ONLY when API returned >0 records** (`range_total > 0`).
- Empty chunks are NOT marked → re-checked next sync. This is intentional: Adobe's API has been observed to temporarily return empty for chunks that genuinely had sales (historical breakage in 2024-2025 silently wiped ~9,500 rows / $8,400 from our DB before we detected it). One extra request per empty month per sync is a tiny price for never losing history again.
- **First-sale guard (ARMED ONLY AFTER A FULL WALK — 2026-06-09 fix):** chunks whose
  `chunk_end < MIN(date) FROM sales WHERE stock='Adobe Stock'` are marked done
  unconditionally — BUT this is gated behind the `_adobe_full_walk` sentinel in
  `_processed_dates.json`. On a COLD START Pass 1 pulls only the recent window, so
  `MIN(date)` is just that window's floor (e.g. 2025-06), NOT the real first sale.
  Trusting it then made Pass 2 mark ALL older chunks "pre-first-sale" → done forever →
  silently locked out years of history (the **73k → 14.9k** data-loss bug). So
  `first_sale_d = None` until Pass 2 reaches the 10-year cutoff uninterrupted once, which
  sets `_adobe_full_walk=True`. To force a full re-walk: delete BOTH the `"Adobe Stock"`
  key and `"_adobe_full_walk"` from `_processed_dates.json`.
- To force a full re-walk: delete `"Adobe Stock"` key from `_processed_dates.json`.
- **⚠️ NEVER add `pv=` or `timestamp=` to a DATE-FILTERED sales-earnings/other-payments
  request (2026-06-10 fix).** With a date filter present, `pv` makes Adobe IGNORE
  `start_date`/`end_date` and return the FULL recent dataset (~48k dupes) → `range_total>0`
  → old months wrongly marked done → years of history skipped (the **$11k Adobe gap**:
  2020-2023 per-sale history silently lost). The real browser sends NEITHER param with a
  date filter (Inspector-verified: `?start_date=2021-01-01&end_date=2022-01-01&time_range=day`
  → 748 recs; 2020 → 105; 2019 → 0). Pass 1 (recent, NO date filter) may keep `pv` as a
  cache-buster — it has no date filter to break.

#### Endpoint variants — don't confuse them
- `/en/insights/sales-earnings` — **individual sales list** (what we use). Accepts date params. Returns `sales.history[]`.
- `/en/insights/earnings` — **daily aggregates only** (totals per day). Don't use for collection; we'd lose per-sale data.

### ✅ Shutterstock
- **Method: API** (`_shutterstock_api_collect`)
- Browser fetch to `/api/next/v2/earnings/media_stats/day?date=YYYY-MM-DD&page=N&per_page=100`
- Header: `x-end-app-name: contributor-web`
- Response: `media[].{mediaId, total, details.previewImageUrl}`
- Iterates last 92 days one by one, dedup by date in `_processed_dates.json`
- **Important:** navigate to `/earnings` first to get auth cookies, then fetch
- Old `_shutterstock_collect` kept as HTML-parse fallback (slow)

### ✅ Getty / iStock
- **Method: API** (`_getty_api_collect`)
- Profile: `getty_profile/` (separate from `chrome_profile/`)
- Start URL: `https://esp.gettyimages.com/contribute/stats`
- Auth: `ccw` cookie (HttpOnly!) → read via `browser.cookies()` (NOT `document.cookie`) → base64 decode → JSON → `sts_token`
- API endpoint: `GET /api/account/v1/statistics/downloads_for_search?fromDate=...&toDate=...&page=N&pageSize=50&orderResultsBy=LastDownloadDate&sortDirection=Descending&primaryDatePeriod=by_month`
- Header: `Authorization: Bearer {sts_token}`
- Response: `{TotalAssetCount, AssetDownloadSummaries[].{MasterId, ThumbnailUrl, Collection, LastDownloadDate}}`
- **No prices in API** — only download counts. Prices only from TSV statements.
- TSV import: `import_getty.py` — parses monthly statement (tab-separated, utf-8-sig)
  - stock name: `"iStockphoto"` (if collection contains "premium"), else `"iStock"`
  - Date: "05-Mar-2026" → "2026-03-05"
- Session dedup: `_processed_dates.json` → key `"Getty/iStock"` → list of `"YYYY-MM"`

### ✅ Envato Elements
- **DONE.** Earnings (monthly aggregate + per-item), clean portfolio thumbnails, pHash grouping. See "✅ Envato — DONE" section below.

### ✅ Freepik (magnific.com) — DONE (2026-06-09, `collectors/freepik.py`)
- Host `contributor.magnific.com`. Auth: **cookie session + header `x-requested-with:
  XMLHttpRequest`, NO CSRF**. `user_id` from `/xhr/user`.
- **Earnings** = `/xhr/stats/download?user_id=&month=MM&year=YYYY` → per-asset monthly
  CSV (`Freepik Asset ID, Downloads, Earnings EUR, File name`). Per-asset data exists
  **only from 2025-01** (all earlier months return empty). Stored as `stock="Freepik"`,
  EUR→USD per-month at the invoice date (frankfurter/ECB, cached `recipes/_fx_eur_usd.json`).
- **Clean thumbnails** = `/xhr/resource/published?limit=100&page=N` → `{id, imgPreview
  (img.magnific.com, watermark-free), downloads (all-time), date}`. `load_img` 400×400 →
  `asset_meta` dHash → groups via the layered display fold (no Pass E/F).
- **pre-2025 estimate** (`_collect_pre_history`): Freepik has no per-asset data before
  2025 — anchor to REAL aggregate monthly revenue `/xhr/stats?format=monthly` (back to
  2021), split per-asset by all-time download weight, one record per (asset, year)
  dated `YYYY-12-31`. `date < 2025-01` = the separable estimate stream. **Collect ONCE**
  (skip if any Freepik `date<2025-01` row exists) — never re-walk like other stocks.
- Gate: invoice validates 4–10th → collect a month only after the 10th; `recipes/
  _freepik_months.json` marks done months (never refetched).

### ⏳ Pond5 / 123RF
- Chrome profiles already created, need: inspect real page + write dedicated collector
- (Depositphotos ✅ done, Envato ✅ done, Freepik ✅ done)

## ⏭️ NEXT SESSION — detailed tasks

### 1. Remove Playwright-based COLLECTION → direct-API + on-demand login window (HIGH)
User: "плейрайт механіки ВЗАГАЛІ не потрібно" / "Адоб після того як треба було б
залогінитись фолбекає на плейрайт — плейрайт механіки ВЗАГАЛІ не потрібно".
- The browser window must open **ONLY** for a manual login (session expired), exactly
  like the Windows version. After login, ALL collection is direct API using the captured
  decrypted cookies (`recipes/_pw_cookies.json` on Mac), never a Playwright page.
- Target flow per stock: `_stock_has_valid_session()` → if OK, run direct-API collect;
  if 401/needs-login → open the login window for THAT stock only → user logs in →
  `_capture_pw_cookies` → retry direct API once.
- Refactor lands in `orchestrator.py` (`_run_collector_global`, `_sync_all_global`) +
  the collectors (drop the `pw_page` collect path; keep only `*_collect_direct`).
- Verify on the INSTALLED app, log `~/Library/Application Support/StockAutomation/app.log`.

### 2. DB re-sync recovery (USER ACTION, already understood)
The dedup data-loss is fixed (see Architecture Invariants). User re-syncs from scratch:
Settings → Full Reset → Sync All. Watch Adobe top-seller download counts match the real
Adobe dashboard (the 6564 vs 1108 proof).

### 3. FIXED (2026-06-10) — group badges not showing in BestSellers/Downloads/Groups after sync

**Root causes (all fixed this session):**

1. **`notifySyncDone()` did not refresh `photoGroups`** (Svelte + Swift).
   - Svelte: added `loadPhotoGroups()` call in `notifySyncDone()` (`appState.js`).
   - Swift: `notifySyncDone()` already called `refresh()` which fetches photoGroups — but
     BestSellersView and GroupsView had no `onChange(of: model.syncTick)` → never reloaded.
     Fixed by adding `.onChange(of: model.syncTick)` to both views.

2. **`/api/photo-groups` returned only ms_library stockids + photo_groups.json primaries** —
   no Envato/Freepik/iStock IDs. Fixed: endpoint now returns `photo_groups.json` + full
   sib_of expansion from `_cross_stock_matches` (pHash clusters). Non-primary asset_ids
   (Envato UUIDs, Freepik numeric IDs, iStock IDs) now map to their group correctly.

3. **Pass B removed** — ms_library stockids clustering was a relic. Matching is now
   **pHash+RGB only** (Pass A + Pass F). `api_photo_groups_get` no longer reads ms_library
   stockids at all.

4. **Freepik: 2615 assets had no thumbnail in `img_cache/`** → no pHash → not matchable.
   Fixed: Freepik collector now backfills missing thumbnails on every sync (step 4 in
   `_freepik_collect`). After next sync + Rebuild Matches → all Freepik photos get pHash
   and will appear in correct groups.

5. **BestSellers multi-select** — added Cmd/Shift+click + "Add to group" action bar
   (identical to Downloads). `TopPhotoCard` now accepts `isSelected` param with accent
   border + scale highlight.

**After restart + sync + Rebuild Matches** all stocks should show group badges.

### 4. FIXED (2026-06-09) — "Sync All: not logged in" false negatives
After login to all windows, Sync All reported the 4 marker stocks (Getty, Envato,
Shutterstock, MS+) as not logged in. Root cause: `_stock_has_valid_session()` GATED on a
hardcoded cookie NAME per stock (`ccw`, `accts_contributor`, `koa.sid`, envato session) —
but those are HttpOnly or minted only on a subdomain the login URL never lands on (Getty's
`ccw` lives on `esp.*`, login lands on `accountmanagement.*`). Fix: the in-app isolated
Chrome profile is the source of truth — marker name is now a FAST POSITIVE only, never a
gate; any live cookie on the stock's domains = logged in. Generic for all current + future
stocks. Also removed the dead Safari-cookies fallback from `_load_browser_cookies` (Mac)
— it was never part of the login flow and spammed "Operation not permitted".

## General API collector pattern (Adobe / Shutterstock / Getty)

```
1. Playwright opens browser with persistent profile
2. Navigate to the stock's earnings page → browser gets auth cookies/tokens
3. page.evaluate("""async () => {
       const r = await fetch("/api/...", {credentials: "include", headers: {...}});
       return await r.json();
   }""")
4. Parse JSON → save_to_db
5. _processed_dates.json → dedup between sessions
```

**Why fetch inside browser instead of requests.get():**
DataDome and similar anti-bot systems block direct HTTP requests. Browser-internal fetch looks like a legitimate request.

## 🔍 Inspector — how to use its logs (EVERY future session, READ THIS)

When you need a stock's API endpoint (new stock, missions, any unknown XHR), DO NOT ask
the user to copy/paste request details by hand. The **user runs the in-app Inspector**
(Browser tab → Inspector → pick a stock → browses/logs in), and it **records EVERYTHING
to disk**. You then dig through those files yourself — they already contain every request,
header, body, and a full curl. Workflow:

1. **Where the logs live:** `<STOCK_DATA_DIR>/inspector_logs/` (on Mac:
   `~/Library/Application Support/StockAutomation/inspector_logs/`). Files:
   - `<Stock>.log` / `Universal.log` — human-readable stream: for each captured request a
     **full `curl '<url>' -X … -H …  --data-raw …`** line PLUS the response body when it
     looks like data. This is usually all you need.
   - `Universal_<timestamp>.har` — full HAR (every request/response). Grep/parse this when
     the `.log` filtered something out.
   - `Universal_cookies.json` — cookies captured during the session.
2. **How to find the endpoint:** grep the `.log` (or `.har`) for the money words —
   `_DATA_HINTS` in `routes/sync.py`: `earning|sale|download|amount|commission|revenue|
   payout|statement|item_id|asset|media|price|balance`. The inspector already DROPS noise
   hosts (`_NOISE_HOSTS`: analytics/datadog/sentry/etc.) and only dumps bodies that
   `_looks_like_data()` (JSON starting with `{`/`[` containing a data hint).
   Example: `grep -i payout ~/Library/Application\ Support/StockAutomation/inspector_logs/Universal.log`
3. **Then:** lift the URL + required headers from the curl, replicate as a browser-internal
   `fetch()` (see pattern above) or direct `requests` with captured cookies, and write the
   collector. The `.log` curl IS the source of truth — don't guess endpoints.
4. The user's job is only "run Inspector and browse the relevant page"; YOUR job is reading
   `inspector_logs/` and extracting what you need. Don't make them transcribe.

## Photo groups & cross-stock matching

### ⚠️ LAYERED GROUPING MODEL — every new stock MUST obey this (2026-06-09)
This is the load-bearing contract. A new stock is **purely additive**; it must NOT
change any layer below. Freepik broke this once (see below) — don't repeat it.

1. **DB layer — each stock separate.** Every stock's photos live independently in
   `sales` + `asset_meta` (pHash `thumb_hash`, aspect, RGB). A new stock just adds
   its own rows. Nothing else changes.
2. **Group structure layer — MS+ only.** `photo_groups.json` holds the MS+/ms_library
   **shoot membership ONLY** — one primary stockid per photo, grouped by ms_library
   `group`. This is the authoritative group composition. **NEVER write another
   stock's asset_ids (siblings, Freepik, visual matches) INTO photo_groups.**
3. **Matching/fold layer — at DISPLAY.** `api_groups_get` folds every stock's version
   of a photo into ONE card via `_cross_stock_matches` (it expands the `represented`
   set + each card's earnings over the photo's cross-stock cluster). A new stock
   appears in cards automatically once its `asset_meta` pHash-clusters into
   `_cross_stock_matches` (Pass A). No per-stock code in the group structure.
4. **Manual overrides layer — last.** `_match_overrides.json` (Pass H) wins on top.

**THE REGRESSION (don't redo):** rebuild **Pass E** (append cross-stock siblings into
photo_groups) and **Pass F** (append MS+ visual matches into photo_groups) violated
layer 2 — they stored other stocks' ids in `photo_groups`. With ms_library stocks it
was invisible (all siblings were ms_library ids → collapsed by `represented`), but
Freepik (NOT in ms_library) surfaced it: thousands of wrong-shoot photos bloated
groups (one group went 339 → 3495, mixing shoots; manual removal wouldn't stick).
**Both Pass E and Pass F appends are now DISABLED** — folding happens only at display.
If you add a stock and groups bloat/mix, you re-introduced a write into photo_groups.

- `photo_groups.json` — user-managed groups. Stores **only ONE primary stockid per photo** (no siblings — that would cause duplicates in the UI).
- `ms_library.json` — 14 000+ photos with `{filename, group, stockids: {adobestock, shutterstock, istock, esp, ...}}` — source of truth for cross-stock links.
- `_cross_stock_matches.json` — maps `primary_id → [primary_id, sibling_id, ...]`, rebuilt by pHash+RGB matching only. Used by `/api/photo-groups` (expanded to siblings) and `api_groups_get` to aggregate earnings across sibling IDs.
- **Rebuild flow** (`/api/rebuild-matches`): reads ms_library.json → groups photos by `group` field → for each photo picks ONE primary_id (adobestock > shutterstock > istock > esp) → writes to photo_groups.json.
- **Dedup**: `api_rebuild_matches` has a dedup pass; `api_groups_get` tracks a `represented` set to skip siblings already covered by their primary.

## Architecture Invariants — DO NOT BREAK

These are load-bearing contracts cemented across multiple debugging sessions.
Each was a real bug that took hours to trace. Future agents: read this before
touching any of these areas.

### Sync orchestration (`_sync_all_global`, main.py:~2740)
- **Parallel mode only when DB has >1000 sales.** Cold-start (empty DB) MUST go
  sequential — 4 parallel collectors hammering an empty DB triggers fd
  exhaustion / rate limits / SQLite contention. The guard is at the start of
  `_sync_all_global`. Don't remove.
- **MS+ runs LAST, after all 4 sales stocks.** MS+ is the slow one (15k photos,
  refs in `img_cache_ms/`). User sees sales fast; MS+ updates `ms_library.json`
  in background which only matters for the post-sync rebuild-matches.
- **`_session_new_keys` is cleared at sync start, appended by `_save_record` per
  insert.** Client reads via `GET /api/sync/recent-keys` to paint blue
  highlights. DO NOT switch back to client-side diff — that's been broken twice
  due to closure / cache timing issues.

### Collectors — `_save_record` is the ONLY save path (main.py:359)
- Collectors call `_save_record(rec)` (sync, in-process). NEVER use the old
  HTTP `_http_executor.submit(requests.post(...))` pattern — sync thread would
  finish before background HTTP POSTs hit the DB → SSE `done` fires with stale
  data.
- `_save_record` has a dedup guard (`is_already_saved`) BEFORE insert. Without
  it, re-syncs would balloon `_session_new_keys` with already-known sales.
- Date normalization: 5 input formats supported. Stored as 19-char
  `"YYYY-MM-DD HH:MM:SS"`. `/api/feed` slices to 10-char for display.

### Dedup — `is_already_saved` (db.py) — REWORKED 2026-06-09 (was destroying data)
- **TIMESTAMPED incoming sale (Adobe, has HH:MM:SS): EXACT 19-char match ONLY.**
  Different sales of the same photo on the same day at the same price have different
  timestamps → they are DISTINCT. The old code fell through to a `substr(date,1,10)`
  + price±0.005 day-match that treated every extra same-day same-price sale as a dup
  → dropped real Adobe subscription sales (a 6564-download top-seller collapsed to
  ~1108). NEVER reinstate the day+price fallback for timestamped sales.
- **DATE-ONLY incoming sale (SS / Depositphotos daily aggregates): day+price fallback**
  is still used (those have no time component; this dedups re-synced aggregates).
  Still **DO NOT add `AND LENGTH(date)=10`** (separate historical bug).
- `/api/deduplicate` was even worse: `GROUP BY stock, asset_id, DATE(date)` kept ONE
  row per (photo, day) regardless of price/count → mass data loss across ALL stocks.
  Now `GROUP BY stock, asset_id, date, price` (true exact dups only). **If you ever
  re-aggregate by day you will silently destroy real sales — don't.**
- Index `idx_sales_dedup ON sales(stock, asset_id, date)` is required for performance.

### pHash matching is numpy-vectorised (`matching_engine.py`, 2026-06-09)
- `_hash_based_matches` packs `thumb_hash` into a `uint64` array and finds candidates
  via `XOR + popcount` (256-entry LUT) + masked stock/aspect/RGB filters, instead of
  the old O(n²) pure-Python pair loop. 35k asset_meta: ~5 min → ~35 s, SAME verdict
  (deterministic). Pure-Python fallback kept if numpy is missing. Flask dev server is
  single-threaded → a rebuild still blocks the backend for those ~35 s (GIL means
  threads won't help CPU matching; further speedup = LSH/prefix bucketing, not GPU).

### Adobe Stock — Pass 1 / Pass 2 contract (main.py:559+)
See `### ✅ Adobe Stock` section above. Key invariants:
- `stop_pages` early-exit in Pass 1 MUST `break`, not `return` (this killed Pass 2 silently for months).
- Pass 2 chunks: **360 days** (Adobe max is 364, 4-day safety margin).
- **Don't mark empty chunks as done.** This is what lost us $8,400 of history in 2024-2025. See Adobe section.

### macOS cookies — Playwright cache, NOT profile decryption (2026-06-08, `cookies.py`)
- macOS app Chrome/Chromium profiles are written with `--use-mock-keychain` /
  `--password-store=basic`, so their **v10 cookies are NOT decryptable** by either the
  `Chrome Safe Storage` or `Chromium Safe Storage` Keychain key. `_load_appprofile_cookies_mac`
  returns ~0 → collectors logged "no browser cookies" → Playwright fallback → DataDome.
- FIX: capture cookies straight from the Playwright **context** — `context.cookies()`
  returns them ALREADY DECRYPTED (httpOnly included) — into `recipes/_pw_cookies.json`.
  `_load_browser_cookies()` reads this cache FIRST on macOS. Capture happens in the login
  window (`_wait_close_capturing`), the per-stock login flow, and after every Playwright
  collector run (`_capture_pw_cookies(browser)` in orchestrator). Self-healing: first
  Playwright sync/login populates the cache → subsequent syncs use the fast direct API.
- Keychain lookup must query by SERVICE only (`-s 'Chrome Safe Storage'`), NOT
  `-a 'Chrome Safe Storage'` (that's the wrong account → fails → only the useless
  'peanuts' key remained). `_mac_chrome_candidate_keys` tries both Chrome + Chromium keys.
- `_stock_has_valid_session()` (markers per stock; Envato uses `envatosession`/`envatoid`,
  NOT the HttpOnly `_author_warehouse_session`) → Import-Cookies opens login tabs ONLY for
  not-logged-in stocks.

### Stealth must NOT touch real Chrome — even on macOS (`collectors/browser.py`)
- `_apply_stealth(ctx, real_chrome=False)` and the spoofed Mac UA are ONLY for bundled
  Chromium. When driving real system Chrome (`channel='chrome'`, e.g. Shutterstock),
  injecting stealth JS + a fake UA on top of a genuine Chrome fingerprint is exactly what
  DataDome flags and POISONS the datadome token — this kept blocking Shutterstock on Mac.
  Real Chrome / `channel='chrome'` → pass `real_chrome=True` everywhere (login flow,
  `_run_collector_global`, `_windows_browser_login`). Mirrors Windows (no stealth there).

### Shutterstock (main.py:_shutterstock_api_collect_global)
- Browser fetch to `/api/next/v2/earnings/media_stats/day` (not direct HTTPS — DataDome blocks). Navigate to `/earnings` first to seed cookies.
- Headless mode requires `networkidle` + `add_init_script(_STEALTH_JS)`. Without them → `/unsupported-browser`.
- Header `x-end-app-name: contributor-web` is mandatory.
- Incremental dedup: last 30 days via DB MAX(date) check (saves ~92→30 daily requests). Cold-start: full 92 days.
- **Resumable historical backfill (`collectors/shutterstock.py`, 2026-06-09).** SS exposes
  every month/year back to signup (dashboard year picker → 2015), so all-time IS
  collectable. The direct collector now does TWO phases: (1) a RECENT forward scan from
  `MAX(date)-1` (or last 45d if empty), then (2) a BACKFILL walking BELOW `MIN(date)`
  toward `SS_FLOOR=2015`, resumable via `_processed_dates.json` keys
  `Shutterstock_backfill_cursor` / `Shutterstock_backfill_done`. A 3× consecutive 403
  (DataDome) during backfill PERSISTS the cursor and returns True (NOT False — that would
  fall back to Playwright and lose the cursor), so the next sync resumes. **The old bug:**
  once any SS row existed, sync went incremental from `MAX(date)` FORWARD only and never
  backfilled — an interrupted cold-start walk left SS recent-only forever (3809 vs 36847).
  To force a full re-backfill: delete both backfill keys from `_processed_dates.json`.

### Getty / iStock (main.py:_getty_collect_direct + _getty_api_collect_global)
- ESP profile is SEPARATE (`getty_profile/`), not `chrome_profile/`.
- `ccw` cookie is HttpOnly — read via Playwright `browser.cookies()`, NEVER `document.cookie`.
- Base64-decode `ccw` → JSON → `sts_token`. Pass as `Authorization: Bearer {sts_token}`.
- API returns at most 50 assets per request. Paginate by `TotalAssetCount / 50`.
- Skip if last sync was THIS calendar month and we're before the 21st (statements arrive ~21st).
- Prices come from TSV statements only (API returns counts, no prices). Import via `import_getty.py`.

### Depositphotos (main.py:_depositphotos_collect_direct)
- HTML scraping via BeautifulSoup (no JSON API).
- Thumb URLs are protocol-relative (`//st.depositphotos.com/...`) — MUST prepend `https:` (or images break on cards). Both `_extract_rows` functions do this normalization.
- First-time vs incremental: cold-start = 500 pages, warm = 30 pages.

### MS+ (Microstock+) (main.py:_microstock_plus_collect_direct)
- Auth: Safari `binarycookies` parsing (cookies live in `~/Library/Containers/com.apple.Safari/`). Required: `koa.sid` or `session_debug`.
- `useragencyids` parameter is URL-encoded JSON array, NOT PHP-style repeated keys (the old `useragencyids[]=foo&useragencyids[]=bar` returns HTTP 500).
- Two-tier diff (v0.9.36+):
  1. `recipes/_ms_dirs_state.json` stores `{path: filestotal}`. Next sync fetches `contentlist` only for dirs whose `filestotal` changed.
  2. Thumbnail download iterates ONLY new basepaths (not all 15k). Skip pHash compute when `dl_count == 0`.
- Basepath is the unique key (NOT filename — camera reuses `B94A1234` every 10k shots).

### rebuild-matches (main.py:5044, `api_rebuild_matches`)
Pass ordering and incremental contracts (v0.9.37):
- **Pass A (pHash clustering)** — incremental via `last_asset_meta_rowid` cursor in `recipes/_matches_state.json`. Old × old pairs skipped (hashes are immutable, threshold constants).
- **Pass B (ms_library stockids) — REMOVED.** Cross-stock matching is pHash+RGB only (Pass A + Pass F). ms_library stockids are no longer used for clustering.
- **Pass D (MS+ groups → photo_groups) — SNAPSHOT DIFF.** `recipes/_ms_group_snapshot.json` stores `{primary_id: ms_group_name}` from last successful rebuild. Next rebuild computes `diff_added` and `diff_moved`. **Unchanged primaries are not touched — this is how user UI moves survive across rebuilds.**
- **Pass F (MS+ visual matching)** — incremental with BOTH cursors (`last_asset_meta_rowid` + `last_ms_meta_rowid`). Pairs where both rows are old → skip (60M → ~10k comparisons typical).
- **Full early-exit** at start: if asset_meta + ms_meta + ms_library fingerprint all unchanged → return cached counts immediately (no work).
- **First-run path** for Pass D (empty snapshot): does CLASSIC full purge + reassign, NOT diff. Otherwise photos end up in two groups. Don't change this.
- Manual overrides (`_apply_manual_overrides`, Pass H) always wins. Stored in `recipes/_match_overrides.json` via `/api/match-override`.
- To force full rebuild: delete `recipes/_matches_state.json` and `_ms_group_snapshot.json`.

### SQLite (main.py:init_db)
- **WAL mode is required** (`PRAGMA journal_mode=WAL`). Without it parallel sync collectors block each other.
- `synchronous=NORMAL` is safe with WAL.
- Indexes: `idx_sales_dedup`, `idx_sales_date`, `idx_asset_meta_hash`, `idx_ms_meta_hash`. Don't remove.
- Connection timeout 15s (60s for rebuild-matches batch ops). All connections use `with sqlite3.connect(...)` for autocommit-on-exit.

### Svelte UI invariants
- `appState.js` is the **single source of truth**. Tabs subscribe to writables, never duplicate fetches. New shared state → add a writable here, not a prop.
- `syncTick` is incremented by `notifySyncDone()` after any sync completes. Every tab has a `$effect` watching it that reloads cache-skipping (`load(true, true)`).
- `notifySyncDone()` ALSO **clears tab caches** (`_dlMem`, `_bsMem`, `_groupsLoaded`). If a tab needs cache to survive sync (e.g., to preserve blue highlights), it MUST re-`downloadsCache.write(...)` AFTER calling `notifySyncDone()`. See Downloads.svelte `doRefresh` → `finish` for the canonical pattern.
- **Blue new-sale highlights:** backend-tracked via `_session_new_keys` (Python side). Client reads via `GET /api/sync/recent-keys` → `newSaleKeys.set(new Set(...))`. **Do NOT switch to client-side diff** — it was broken twice (closure timing, cache invalidation). The backend-tracked path is the only reliable one.
- Each card's `isNew` is `$newSaleKeys.has(_k(item))` where `_k(it) = "asset_id|date|stock|price.toFixed(2)"`. Backend produces matching keys in `_save_record`.

### Group merge UI (Groups.svelte, v0.9.39)
- Optimistic local mutation (`_localMerge`) — merges photos into target, removes source from `groups` array. Server call fires in background.
- `animate:flip` on each group card → other cards smoothly reposition.
- Custom `mergeFly` outro captures target rect BEFORE mutation, source card scales + translates toward it.
- Full reload via `load({ force: true })` only on server error (rollback).

### Stock UI metadata (Stocks tab) — when adding a new stock
1. Add chrome profile dir `chrome_profile_StockName/`.
2. Add to `STOCK_URLS` (sync landing) and `_INSPECTOR_URLS` (inspector deep-link).
3. Add to `_stock_colors` in `recipes/stock_colors.json` (or default).
4. Add to `INSPECTOR_STOCKS` array in `Browser.svelte`.
5. Add to `STOCKS` array in `Browser.svelte` (filter pills).
6. Write `_stockname_api_collect(pw_page)` following the Adobe/SS pattern.
7. Wire into `_sync_all_global` SALES_STOCKS list.

## Pitfalls from experience

### Getty/iStock — critical mistakes to avoid

1. **`document.cookie` won't give `ccw`** — it's HttpOnly. Always read via `browser.cookies(["https://esp.gettyimages.com"])` and find `c["name"] == "ccw"`.
2. **`media.gettyimages.com/internal/{id}?...` is a signed, temporary URL** — ok to store in DB, but may expire. Download to `img_cache/` immediately via `load_img_async`.
3. **Building Getty CDN URL manually (`/id/{id}/photo/{slug}.jpg`) won't work.** Getty requires a signed `c=` parameter (HMAC). Only get `ThumbnailUrl` from API.
4. **`chrome_profile` and `getty_profile` are different.** Getty ESP auth lives in `getty_profile/`. Don't mix them.
5. **ProfileSingleton error** — if browser is still open (background process), a new `launch_persistent_context` will fail. Always wait for previous browser to close.
6. **ESP stats page returns max 50 assets per request.** Paginate: `TotalAssetCount / 50` = pages. Monthly range API may return only unique assets (not individual sales). Prices must come from TSV.

### Shutterstock
- Headless: `networkidle` + `add_init_script(_STEALTH_JS)` are mandatory — without them redirects to `/unsupported-browser`
- Prices: comma as decimal separator → `.replace(',', '.')`

### General
- **Playwright ProfileSingleton**: one profile = one browser at a time
- **Session dedup**: `_processed_dates.json` → to re-sync from scratch, delete the file or the specific key
- **SQLite 999 variable limit** → batch queries in chunks of 800 via `_query_earnings_batch()`
- **IntersectionObserver in Svelte 5**: use `$effect` (not `onMount`) to attach the observer, so it re-runs when the sentinel element is recreated by reactive blocks

## How to add a new stock

1. Log in to the stock's contributor page in the dedicated `chrome_profile_StockName/` browser
2. Open DevTools → Network → find API endpoints returning JSON sales data
3. Note the endpoint, required headers, response format
4. Write `_stockname_api_collect(pw_page)` following the pattern above
5. Wire into `_sync_all_thread`: add stock name to the loop and call the collector
6. Add stock name to `STOCK_LIST` in Flask for the stock-list API

## Svelte UI — what's implemented vs what's missing from Flet

### Architecture (post-refactor, 2026-05-19)

All tabs share **one central state store** at `$lib/stores/appState.js`. Tabs are thin
views over this store — no more prop drilling, no more duplicated fetches.

```
src/lib/
├── stores/
│   └── appState.js          ← single source of truth
│       writables: stockList, matches, photoGroups, syncTick
│       methods:   loadStockList, loadMatches, saveMatches,
│                  loadPhotoGroups, toggleGroupMember, createGroup,
│                  notifySyncDone
├── utils/
│   └── matching.js          ← pure functions: linkMatches, unlinkFromMatches, getSiblings
├── components/
│   ├── FilterPills.svelte   ← shared period/stock/sort pill row
│   └── GroupContextMenu.svelte  ← shared right-click menu (used by Downloads + BestSellers)
├── PhotoPopup.svelte        ← reads photoGroups from store directly
├── StockColorSettings.svelte
├── stockColors.js           ← writable store for stock colors (separate, dedicated)
├── api.js                   ← thin fetch wrappers (used by stores)
└── tabs/
    ├── Downloads.svelte
    ├── BestSellers.svelte
    ├── Groups.svelte
    ├── Browser.svelte
    └── Analytics.svelte
```

**Cross-tab synchronization:**
When any tab finishes a sync (Downloads' Refresh button OR Browser's Start), it calls
`notifySyncDone()` which increments `syncTick`. Every tab has a `$effect` watching `syncTick`
and reloads its own data when it changes. So sync on Browser tab → Downloads/BestSellers/Groups
all refresh automatically.

When a tab toggles a group membership or creates a group, the central `photoGroups` store
updates → all other tabs (and PhotoPopup) immediately see the change.

### Implemented ✅

| Feature | Component |
|---------|-----------|
| Downloads feed (infinite scroll, period + stock filters) | Downloads.svelte |
| Best Sellers by revenue (cross-stock aggregation, auto-match) | BestSellers.svelte |
| Groups tab (list + modal + remove photo + match + rename + delete) | Groups.svelte |
| Analytics charts (timeline + by_stock bars) | Analytics.svelte |
| Browser/sync tab (start/stop, headless toggle, SSE live log) | Browser.svelte |
| Top stat boxes (today/week/month/year + group stats when group open) | +page.svelte |
| Right-click context menu → group management | shared GroupContextMenu.svelte |
| Photo popup (sales history, by-stock breakdown, group membership) | PhotoPopup.svelte |
| Light/dark theme toggle | +page.svelte |
| Refresh button (SSE-driven sync + blue highlight for new sales) | Downloads.svelte |
| FIRST sale ribbon on cards | Downloads.svelte |
| Infinite scroll via IntersectionObserver + `$effect` | All tabs |
| Export / Import DB via native Tauri save dialog | StockColorSettings.svelte |
| Stock color customization (persists to `recipes/stock_colors.json`) | StockColorSettings.svelte |
| **Cross-tab live sync via central store** | appState.js |

### Missing / not yet in Svelte ⏳

| Feature | Notes |
|---------|-------|
| **Search box in Best Sellers** | `/api/sales` supports `?q=` but no UI input |
| **Sort by count in Best Sellers** | `/api/sales` supports `?sort=count` but no UI toggle |
| **`/img/ms/<fname>` images** | Route in `api.js` but no component calls `imgMsUrl()` |
| **`/api/ms-library/remove-from-group`** | Exists in backend; Groups.svelte uses `onToggleGroup` instead (works but doesn't update ms_library.json) |

## Roadmap

### ✅ Done
- [x] Adobe Stock — API collection
- [x] Shutterstock — API collection (headless + visible)
- [x] Getty/iStock — API collection via ESP + TSV import
- [x] Flet UI fully removed, replaced with SvelteKit + Tauri
- [x] Tauri auto-starts and auto-kills main.py on app open/close
- [x] Downloads tab — paginated feed (50/page), infinite scroll, period + stock filters
- [x] Best Sellers tab — top photos by revenue, auto-match button, cross-stock aggregation
- [x] Groups tab — photo folders, infinite scroll (fixed), group stats in top stat boxes
- [x] Cross-stock photo matching from ms_library.json (Rebuild button)
- [x] In-modal Match button inside Groups for manual cross-stock linking
- [x] Context menu (right-click) for group management in Downloads + BestSellers
- [x] New sales highlighted in blue after Refresh
- [x] Light/dark theme toggle
- [x] Semaphore(5) for image downloads
- [x] Duplicate photo fix (photo_groups stores only primary IDs)
- [x] IntersectionObserver fix via $effect (was broken inside reactive blocks)
- [x] All UI text translated to English
- [x] Dead code removed (main.py: 9240 → 2218 lines after multiple rounds of cleanup)
- [x] **Full modularization complete (v0.9.51):** main.py 6444 → 158 lines (−98%). All routes in `routes/` blueprints. Module map: utils.py, db.py, sync_state.py, cookies.py, image_utils.py, matching_engine.py, orchestrator.py, config.py, app_globals.py, collectors/, routes/
- [x] **Smoke test script** `test_api.py` — 16/16 endpoints, runs against live app
- [x] **UI:** removed redundant Refresh Stats button from topbar
- [x] **UI:** removed stock-match dots from Downloads cards (belong in BestSellers only)
- [x] **Perf: incremental sync** — Adobe Pass 1 and Shutterstock use MAX(date) from DB. Adobe ~1.5min → ~5-10sec after first run
- [x] **Perf: Depositphotos** — limit=160/page (4x fewer requests), row-level date stop, SQL outside loop
- [x] **BestSellers multi-select** — Ctrl+click selects cards, select-bar with searchable group dropdown, batch add to group/create new
- [x] **BestSellers ungrouped filter** — "Ungrouped" pill button, infinite scroll fills viewport properly
- [x] **GroupContextMenu batch mode** — right-click on multi-selected shows "N photos selected", autofocus search
- [x] **Groups optimistic delete/rename** — instant local update without full reload (like merge)
- [x] Stale project files cleaned up
- [x] **Removed 123RF + Dreamstime collectors entirely** (code + DB rows + UI refs)
- [x] **Export / Import DB** with native Tauri save dialog (`@tauri-apps/plugin-dialog`+`plugin-fs`)
- [x] **Stock color customization UI** (persists to `recipes/stock_colors.json`)
- [x] **Mobile web UI** (briefly added, removed at user request — easier to use Cloudflare Tunnel)
- [x] **Security/threading audit:** fixed SQL injection in `/api/stats` (parameterized queries),
      thread-safety on `_headless_mode` and `_inspector_log` (added `threading.Lock`),
      SSE generator handles `GeneratorExit`, log file closed via `atexit`,
      `is_already_saved` guards empty `asset_id`, deprecated `tempfile.mktemp` replaced,
      EventSource cleanup on component unmount in all tabs.
- [x] **Svelte refactor — central store architecture:**
      - All tabs read from `$lib/stores/appState.js` (no more prop drilling)
      - Extracted `FilterPills.svelte` + `GroupContextMenu.svelte` shared components
      - Extracted `matching.js` pure helpers (linkMatches / unlinkFromMatches / getSiblings)
      - **Cross-tab live sync via `syncTick`** — any sync completion (Downloads Refresh OR
        Browser Start) increments the tick, all tabs auto-reload their data.

### 🔜 Next sessions

#### Modularization ✅ COMPLETE (2026-06-02)
**main.py: 6444 → 284 lines (−96%). All routes extracted to blueprints.**
**git tag `pre-modular-20260601-112644` = safe rollback point.**

**Step 1 — utils.py ✅ DONE**
Pure functions with zero app dependencies moved to `utils.py`:
- `_adobe_clean_thumb_url`, `_hamming_hex`, `_dhash_from_path`, `_dpapi_unprotect`
- `main.py` imports them: `from utils import ...`
- `_parse_safari_binarycookies` stays in main.py (more complete version with httpOnly/expires fields)

**Step 2 — db.py ✅ DONE**
`init_db`, `save_to_db`, `is_already_saved` moved to `db.py`.
DB_NAME computed from STOCK_DATA_DIR env var (same logic as main.py). Both resolve to same path.
main.py: `from db import init_db, is_already_saved, save_to_db`

**Step 2.5 — sync_state.py ✅ DONE**
Shared mutable sync state moved to `sync_state.py` (no circular imports):
- `_sync_state`, `_sync_stop_flag`, `_sync_all_active`
- `_session_new_keys` + lock, `_sync_log_lock`
- `_sync_log`, `_save_record`
main.py imports all of the above from sync_state.py.
`_headless_mode` + `_headless_lock` stay in main.py (only used by orchestrators + Flask route).

**Step 3 — collectors/ ✅ DONE**
All collectors extracted. Final package structure:
- `collectors/__init__.py` — empty
- `collectors/browser.py` — `_STEALTH_JS`, `_apply_stealth`, `_is_login_url`, `_open_browser_context`, `_do_login_flow_global`
- `collectors/adobe.py` — `_adobe_collect_direct`, `_adobe_api_collect_global`
- `collectors/shutterstock.py` — `_shutterstock_api_collect_direct`, `_shutterstock_api_collect_global`
- `collectors/getty.py` — `_getty_collect_direct`, `_getty_api_collect_global`
- `collectors/depositphotos.py` — `_depositphotos_collect`
- `collectors/ms_plus.py` — `_ms_plus_collect_direct`, `_ms_plus_collect_global`

Orchestrators (`_run_collector_global`, `_collect_one_stock_global`, `_sync_all_global`) stay in main.py.

**cookies.py ✅ DONE**
All browser cookie helpers extracted from main.py to `cookies.py`:
`_load_browser_cookies`, `_parse_safari_binarycookies`, `_decrypt_chrome_cookie_db`,
`_load_appprofile_cookies_windows`, `_inject_cookies_via_playwright`,
`_import_cookies_for_stock`, `_windows_browser_login` + related helpers/constants.
Collectors import `_load_browser_cookies` directly from `cookies` (no more circular lazy import).

**⚠️ TAURI BUNDLING — REQUIRED after adding any new module:**
- Every new `.py` file or package MUST be added to `tauri.conf.json` → `bundle.resources`
- Directories (like `collectors/`) are added as a single entry: `"../../collectors"` — Tauri preserves the directory structure
- Individual files: `"../../db.py"`, `"../../cookies.py"` etc.
- After adding: rebuild (`npm run tauri build`) + reinstall (`cp -r ... /Applications/`)
- Verify with: `find "/Applications/Stock Automation.app/Contents" -name "*.py"`
- Symptom of missing module: backend fails silently, some tabs show cached data, others empty

**⚠️ TESTING PROTOCOL (apply after every module extraction):**
1. `python3 -c "import ast; ast.parse(open('NEW_FILE.py').read())"` — new file syntax OK
2. `python3 -c "import ast; ast.parse(open('main.py').read())"` — main.py syntax OK
3. Flask start: `python3 main.py` for 6s — no ImportError in output
4. API smoke test: `python3 test_api.py` — 15/15 green (requires Flask running)
5. Build + install: `npm run tauri build` → `cp -r "...bundle/macos/Stock Automation.app" /Applications/`
6. Only then commit. No exceptions.

**matching_engine.py ✅ DONE**
Pure hash matching functions extracted from main.py:
- `_hash_based_matches` — pHash clustering with incremental rowid cursor
- `_ms_fname_to_libentry` — disk filename → ms_library entry lookup
- `_ms_visual_matches` — MS+ visual matching (pHash + RGB delta)
- `_filename_fallback_matches` — filename-based fallback matcher
- `_apply_manual_overrides` — apply user link/unlink overrides
Flask routes (`api_rebuild_matches`, `api_compute_hashes`, `api_compute_ms_hashes`,
`api_refresh_istock_thumbs`) stay in main.py. `load_ms_library` bridged via lazy import.

**image_utils.py ✅ DONE**
Image/library helpers extracted from main.py:
`_save_asset_meta`, `load_img`, `load_match_thumb`, `load_img_async`,
`_img_executor`, `_http_executor`, `load_groups`, `save_groups`,
`load_ms_library`, `save_ms_library`.
All collectors import `load_img_async` directly from `image_utils` (no more circular lazy import).

**orchestrator.py ✅ DONE**
Sync orchestration extracted from main.py (~320 lines):
`_run_collector_global`, `_collect_one_stock_global`, `_sync_all_global`.
`STOCK_URLS` imported from `config.py`. Lazy imports remain for `flask_app` + `api_rebuild_matches`
(circular dep — resolved in Step 5 via Blueprint callback pattern).

**config.py ✅ DONE**
Central constants: `STOCK_URLS`. Previously duplicated in main.py + orchestrator.py.

**sync_state.py** — also contains `_app_log` + log file handle (moved from main.py).

**Final state: main.py ~284 lines** (was 6444 at start — **−96%**).
Module map:
| File | Contents |
|------|---------|
| `config.py` | `STOCK_URLS` |
| `utils.py` | Pure utils: `_adobe_clean_thumb_url`, `_hamming_hex`, `_dhash_from_path`, `_dpapi_unprotect` |
| `db.py` | DB layer: `init_db`, `is_already_saved`, `save_to_db` |
| `sync_state.py` | Sync state + `_app_log` + log file: `_sync_state`, `_sync_stop_flag`, `_sync_log`, `_save_record`, `_session_new_keys` |
| `cookies.py` | Browser cookies: Safari binarycookies, Chrome decrypt, `_load_browser_cookies`, import flows |
| `image_utils.py` | Image cache: `load_img`, `load_img_async`, `load_match_thumb`, `load_ms_library`, `save_ms_library`, `load_groups`, `save_groups` |
| `matching_engine.py` | pHash matching: `_hash_based_matches`, `_ms_visual_matches`, `_filename_fallback_matches`, `_apply_manual_overrides` |
| `orchestrator.py` | Sync orchestration: `_run_collector_global`, `_collect_one_stock_global`, `_sync_all_global` |
| `collectors/browser.py` | Playwright helpers: stealth JS, `_open_browser_context`, `_do_login_flow_global` |
| `collectors/adobe.py` | Adobe collector |
| `collectors/shutterstock.py` | Shutterstock collector |
| `collectors/getty.py` | Getty/iStock collector |
| `collectors/depositphotos.py` | Depositphotos collector |
| `collectors/ms_plus.py` | Microstock+ collector |
| `main.py` | Flask app init + blueprint registration (~284 lines) |
| `app_globals.py` | Shared path constants + `_query_earnings_batch`, `_load_matches`, helpers |
| `routes/images.py` | `/img/*` endpoints |
| `routes/feed.py` | `/api/feed`, `/api/sales`, `/api/stats`, `/api/stock-*`, `/update` |
| `routes/sync.py` | `/api/sync/*`, `/api/inspector/*`, `/api/debug/log` |
| `routes/groups.py` | `/api/groups*`, `/api/photo-groups*`, `/api/group-*`, `/api/ms-library/*` |
| `routes/matching.py` | `/api/rebuild-matches`, `/api/compute-*`, `/api/matches*`, `/api/match-override` |
| `routes/admin.py` | `/api/export`, `/api/import-*`, `/api/reset*`, `/api/deduplicate`, `/api/rebuild-*` |

---

## Step 5 — Flask Blueprints ✅ COMPLETE (2026-06-02)

**Goal:** main.py → ~200 lines (init + blueprint registration only).
**Rule:** move code verbatim, no logic rewrites. Test after each blueprint.
**Circular dep fix:** `_sync_all_global(rebuild_fn=None)` — pass `api_rebuild_matches` as callback from `api_sync_start` instead of lazy import.

### Blueprint plan

| File | Routes | ~Lines |
|------|--------|--------|
| `routes/__init__.py` | empty | — |
| `routes/feed.py` | `/api/feed`, `/api/sales`, `/api/stats`, `/api/stock-list`, `/api/stock-colors`, `/update` | ~250 |
| `routes/groups.py` | `/api/groups`, `/api/group-photos`, `/api/photo-groups/*`, `/api/groups/match-*`, `/api/ms-library/*`, `/api/group-names` | ~450 |
| `routes/sync.py` | `/api/sync/*`, `/api/inspector/*` | ~300 |
| `routes/matching.py` | `/api/rebuild-matches`, `/api/matches`, `/api/match-override`, `/api/compute-*`, `/api/refresh-istock-thumbs` | ~400 |
| `routes/admin.py` | `/api/export`, `/api/import-raw`, `/api/import-chrome-cookies`, `/api/reset*`, `/api/full-reset`, `/api/deduplicate`, `/api/rebuild-*`, `/api/rebuild-from-db` | ~500 |
| `routes/images.py` | `/img/cache/<aid>`, `/img/placeholder`, `/img/ms/<fname>`, `/img/stock-icon/<name>` | ~120 |

### Shared deps each route file will need
```python
from flask import Blueprint, request, jsonify, abort, send_file
from db import DB_NAME
from image_utils import load_ms_library, load_groups, save_groups, load_img_async
from matching_engine import _apply_manual_overrides, _hash_based_matches, ...
from sync_state import _sync_log, _sync_state, _sync_stop_flag, ...
```

### Globals that stay in main.py (used by routes via import)
- `flask_app` — the Flask app instance (Blueprints register to it)
- `_BASE_DIR`, `RECIPES_DIR`, `CACHE_DIR`, `MS_CACHE_DIR`, etc. — path constants
- `_headless_mode`, `_headless_lock` — sync toggle
- `_RELEVANT_STOCKS`, `_STOCK_KEY`, `MATCHES_FILE`, `GROUPS_FILE` etc.

### Approach for shared globals
Create `app_globals.py` with all path constants + shared state that routes need:
```python
# app_globals.py — path constants and shared Flask-level state
import os
_BASE_DIR = os.environ.get("STOCK_DATA_DIR", ...)
CACHE_DIR = ...; RECIPES_DIR = ...; etc.
MATCHES_FILE = ...; GROUPS_FILE = ...; etc.
_RELEVANT_STOCKS = {...}
```
Routes import from `app_globals`. No circular deps (app_globals has no Flask/route imports).

### Order of extraction (safest first)
1. `routes/images.py` — simplest, no shared state
2. `routes/feed.py` — read-only DB queries
3. `routes/sync.py` — sync state only
4. `routes/groups.py` — most complex, save for later
5. `routes/matching.py` — calls matching_engine functions
6. `routes/admin.py` — biggest, most dangerous

### Circular dep resolution for orchestrator.py
Before doing Blueprints, fix the last lazy import:
```python
# orchestrator.py — _sync_all_global signature change:
def _sync_all_global(rebuild_matches_fn=None):
    ...
    if rebuild_matches_fn:
        resp = rebuild_matches_fn()
```
```python
# routes/sync.py (or main.py) — pass the function:
from orchestrator import _sync_all_global
from routes.matching import api_rebuild_matches  # after matching blueprint
threading.Thread(target=lambda: _sync_all_global(api_rebuild_matches)).start()
```

### Tauri bundling after Step 5
Add `routes/` directory to BOTH `tauri.conf.json` and `tauri.windows.conf.json`:
```json
"../../routes"
```

### Testing protocol (same as always)
1. `ast.parse` each new routes file
2. `ast.parse` main.py
3. Flask start 6s — no ImportError
4. `python3 test_api.py` — 15/15 green
4. Browser test: open each tab, trigger sync
5. Commit: `"refactor step 5: routes/X.py blueprint"`

#### Auto-update mechanism (high priority — user-requested)
The user wants the desktop app to auto-update from a cloud source. Approach:
1. Host new release `.dmg` (or `.app.tar.gz`) on a static host (GitHub Releases / R2 / S3).
2. Add a `latest.json` manifest with `{ version, url, signature, notes }`.
3. Tauri has a native updater plugin: `tauri-plugin-updater` — wire it in `lib.rs` + `Cargo.toml`,
   point at the manifest URL, check on app start. Requires code-signing keys (Tauri docs).
4. UI: small banner in topbar "Update available — restart to apply".

#### Cloud sync (lower priority — user mentioned)
Cloudflare Tunnel was discussed. Mac is `sleep 0` (pmset) — runs 24/7. Path:
```
brew install cloudflare/cloudflare/cloudflared
cloudflared tunnel --url http://localhost:8000
```
For a permanent URL: register on dash.cloudflare.com (free), bind a domain.

#### New stocks (priority order)
- [x] **Depositphotos** — HTML scraping (no JSON API found). Optimized: limit=160/page, row-level date stop.
- [x] **Envato** — DONE. Earnings (monthly aggregate + per-item), CLEAN portfolio thumbnails, pHash grouping. See "✅ Envato — DONE" below.
- [ ] **Pond5** — after Envato

For each new stock, follow "How to add a new stock" above.

---

## ✅ Envato — DONE (final architecture, 2026-06-03)

**Earnings** (`collectors/envato.py`) — two report APIs on `author.envato.com`, split at the daily boundary:
- `earnings/detail?view=monthly&start_date=…&end_date=…` → MONTHLY AGGREGATE totals, from `monthly_first_record_date` (2021-06). No per-item. → synthetic `envato-YYYY-MM` records (one/month, 2021-06 … boundary).
- `performance/item_performance?shopfront=Elements&start_date=…&end_date=…&page=N` → PER-ITEM, only from `daily_first_record_date` (~2025-05). Boundary read live from the detail response. → per-item records dated to their month.
- Both scope `total_earnings` by date range. State: `recipes/_envato_months.json` `{YYYY-MM: total}`; a month re-collects only when its total changed (settling-delay aware). Grand total ≈ $20,334. Auth: cookie session in `chrome_profile_Envato/`, CSRF from `<meta name="csrf-token">`.
- **Login bug (fixed):** `_is_login_url` matched substring `"auth"` inside `"author.envato.com"` → false "needs_login" + popped a window every sync. Fix: strip `"author."` before matching (collectors/browser.py).

**Clean thumbnails** — `portfolio.envato.com/items/search?status=distributable&page=N&per_page=100` → `{uuid, thumbnail_url, created_at}`. **`uuid == per-item asset_id`** (100% coverage of sold photos), `thumbnail_url` is WATERMARK-FREE → map by ID directly. `load_img()` → `ImageOps.fit→400×400` center-square (same as MS+) so `asset_meta` dHash matches MS+. Collector pages portfolio (newest-first, early-stop), caches map in `recipes/_envato_portfolio.json`. One-time backfill of 8443 thumbs: `envato_clean_thumbs.py`.

**Grouping** — clean thumbs → clean `asset_meta` dHash → existing `_ms_visual_matches` (rebuild Pass F) auto-groups into MS+ groups by pHash. ~88% (7421/8443), hamming ~0, verified. Hash-named filenames irrelevant (match by image). No Gemini/overrides.

**Historical per-photo attribution — intentionally NOT done.** Distributing the $17k aggregate by per-item weight = `weight × 6.15` for every photo → Best Sellers order unchanged, no new signal, +130-256k synthetic rows. Aggregates kept.

**Abandoned (don't revive):** (1) `elements.envato.com/item-uuid-redirect` og:image — watermarked+cropped, broke pHash. (2) GDrive "Family stock" filename matching + Gemini disambiguation — 41% of filenames are hash-names (`<24hex>_withmeta.jpg`), watermark+crop broke pHash (median 32). Superseded by the portfolio clean-thumb API. `_gemini_key.json` kept only for optional cleanup of the ~12% ungrouped.

---

## ⚠️ Envato — historical research notes (pre-portfolio; kept for reference)

### Що відомо
- Профіль: `chrome_profile_Envato/` (вже створений)
- Envato має **власне API** (`api.envato.com`) з OAuth токенами
- Попередня спроба підключення зламала щось в застосунку — причина невідома

### Безпечний план підключення

**Крок 1 — Inspector ПЕРЕД написанням коду**
Відкрити Inspector → вибрати Envato → зайти на `https://author.envato.com/earnings`
Знайти XHR запити до `api.envato.com` або `author.envato.com/api/`

### Результати дослідження (Inspector + Console тести, 2026-06-02)

**API endpoints знайдені:**
- `author.envato.com/reports/api/v1/performance/item_performance?shopfront=Elements&start_date=...&end_date=...&sort_by=total_earnings&sort_direction=desc&new_item_only=false&page=N`
  → `{total_count, total_pages, data:[{item_id(UUID), title, category, filename, url, total_earnings, earnings_change}]}`
- `author.envato.com/reports/api/v1/earnings/total` → загальна сума all-time
- `author.envato.com/reports/api/v1/earnings/detail?view=yearly&...` → розбивка по роках
- Auth: cookie-based (`_author_warehouse_session`) + `x-csrf-token` header
- CSRF token береться з `<meta name="csrf-token">` на сторінці

**Thumbnail ситуація:**
- `item_performance` API НЕ повертає thumbnail URLs
- Author dashboard — таблиця без зображень
- Thumbnail доступний тільки через item page: `elements.envato.com/item-uuid-redirect/{UUID}`
- Всі thumbnails мають watermark (`mark-alpha=18` = 18% opacity, та ж сама `watermark4.png`)
- Підпис `s=` прив'язаний до всіх параметрів → прибрати watermark неможливо
- DataDome блокує Playwright NAVIGATION але НЕ блокує `fetch()` зсередини браузера
- `fetch("/item-uuid-redirect/UUID", {credentials:"include"})` з elements.envato.com → повертає HTML з og:image URL ✓

**Стратегія thumbnail:**
- Не використовуємо Envato thumbnail для відображення
- Матчимо Envato → MS+ по `filename` (API повертає оригінальну назву файлу)
- Для camera-дублікатів (однаковий filename) — pHash disambiguate (watermark не заважає: однакові фото = близький pHash, різні = далекий)
- Після матчу відображаємо MS+ thumbnail (чистий, без watermark)
- Для фото без MS+ матчу — тимчасово watermarked thumbnail

**Стратегія збору (2 етапи):**
1. `author.envato.com` → earnings per item (item_performance API, cookie auth)
2. `elements.envato.com` → thumbnail URLs (fetch() зсередини браузера, XHR не блокується DataDome)

**Архітектура колектора:**
```python
def _envato_collect(pw_page):
    # Step 1: get CSRF token + earnings from author.envato.com
    pw_page.goto("https://author.envato.com/reports/performance")
    csrf = pw_page.locator('meta[name="csrf-token"]').get_attribute('content')
    # fetch item_performance pages (all items, paginated 25/page)
    
    # Step 2: navigate to elements.envato.com ONCE for thumbnail fetch
    pw_page.goto("https://elements.envato.com")
    # fetch thumbnail URLs via page.evaluate(fetch()) — DataDome allows XHR
    
    # Step 3: snapshot-diff logic (see algorithm below)
```

**Snapshot file:** `recipes/_envato_snapshot.json` → `{item_id: total_earnings}`

**Що шукати в Inspector (чеклист):**
1. **Endpoint** — URL (напр. `api.envato.com/v2/market/...`)
2. **Auth** — Bearer header? cookie? CSRF token?
3. **Структура** — поля: item_id, total_earnings, downloads
4. **Річна розбивка ⭐** — чи є `earnings_by_year` або окремий `/yearly` endpoint?
   - Якщо є → перший синк = записи по роках (2021→$450 @ 2021-12-31, 2022→$780 @ 2022-12-31...)
   - Якщо тільки total → перший синк = один запис @ MIN(sales.date)-1день
5. **Пагінація** — offset/page параметр якщо фото 1000+

**Крок 2 — написати колектор в ізоляції**
Створити `collectors/envato.py` за патерном adobe.py/shutterstock.py:
```python
def _envato_collect(pw_page):
    # 1. Navigate to earnings page
    # 2. Fetch API via browser (credentials: 'include')
    # 3. Parse JSON → _save_record()
```
НЕ чіпати main.py, orchestrator.py, config.py поки колектор не протестований окремо.

**Крок 3 — тест в ізоляції**
```python
# test_envato.py — запускати окремо, без Flask
python3 -c "from collectors.envato import _envato_collect; print('import OK')"
```

**Крок 4 — підключення (тільки після успішного тесту)**
1. Додати в `config.py`: `"Envato": "https://author.envato.com/earnings"`
2. Додати в `orchestrator.py` SALES_STOCKS
3. Додати в `Browser.svelte` INSPECTOR_STOCKS і STOCKS
4. Додати в `tauri.conf.json` — нічого не треба (collectors/ вже є)
5. `python3 test_api.py` → 16/16

**Ризики з минулого разу**
- Envato може використовувати CSRF токени або особливі session cookies
- `author.envato.com` і `api.envato.com` — різні домени, cookies можуть не передаватись
- Якщо ламає — `git stash` або `git checkout collectors/envato.py` для відкату

**Rollback**: якщо щось зламалось — `git revert HEAD` або `git checkout main -- collectors/`

### Snapshot-diff алгоритм (фінальна версія, після аналізу Gemini)

```python
is_first_global_sync = not bool(prev_snapshot)  # _envato_snapshot.json порожній/відсутній

# Дата для першого глобального синку: MIN(date) з sales - 1 день
# (щоб не псувати Analytics графіки — Envato впишеться в початок кар'єри)
if is_first_global_sync:
    with sqlite3.connect(DB_NAME) as c:
        r = c.execute("SELECT substr(MIN(date),1,10) FROM sales").fetchone()
        first_date = r[0] if r and r[0] else '2020-01-01'
    from datetime import datetime, timedelta
    hist_date = (datetime.strptime(first_date, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')

for item in api_response:
    aid = str(item['id'])
    new_total = float(item['total_earnings'])
    prev_total = prev_snapshot.get(aid)  # None якщо нове фото

    if prev_total is None:
        delta = new_total
        date = hist_date if is_first_global_sync else today  # нове фото не в перший синк → today
    else:
        delta = new_total - prev_total
        date = today

    # ЗАВЖДИ оновлюємо snapshot (навіть refund!) — інакше наступні дельти будуть неправильні
    new_snapshot[aid] = new_total

    # В DB пишемо тільки додатну дельту
    if delta > 0:
        _save_record({'asset_id': aid, 'price': delta, 'date': date, 'stock': 'Envato',
                      'thumb_url': item.get('thumbnail', '')})
```

**Ключові правила (не порушувати):**
- `is_first_global_sync` = тільки коли snapshot порожній (не "нове фото в звичайному синку")
- Snapshot оновлюється завжди, навіть при від'ємній дельті
- В DB пишемо тільки `delta > 0`
- Дата першого синку = `MIN(sales.date) - 1 день` (не 2010-01-01 — ламає Analytics)
