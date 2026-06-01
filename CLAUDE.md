# Stock Aggregator — CLAUDE.md

## What this is

Desktop sales aggregator for photo stocks. Playwright logs into stock sites, collects sales data, saves to local SQLite DB. Flask — local HTTP server (port 8000). SvelteKit+Tauri — desktop UI.

**Flet has been fully removed.** `main.py` is Flask-only (~5500 lines, modularization in progress). UI is `ui-tauri/` (SvelteKit + Tauri).

**Current version: v0.9.50** (Mac + Windows, GitHub Actions CI builds both).
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
main.py              — Flask backend (API routes + Playwright collectors) [MODULARIZATION IN PROGRESS]
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
- **First-sale guard:** chunks whose `chunk_end < MIN(date) FROM sales WHERE stock='Adobe Stock'` are marked done unconditionally (pre-activity months are guaranteed empty).
- To force a full re-walk: delete `"Adobe Stock"` key from `_processed_dates.json`.

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

### ⏳ Depositphotos / 123RF / Pond5 / Envato
- Chrome profiles already created, need: inspect real page + write dedicated collector

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

## Photo groups & cross-stock matching

- `photo_groups.json` — user-managed groups. Stores **only ONE primary stockid per photo** (no siblings — that would cause duplicates in the UI).
- `ms_library.json` — 14 000+ photos with `{filename, group, stockids: {adobestock, shutterstock, istock, esp, ...}}` — source of truth for cross-stock links.
- `_cross_stock_matches.json` — maps `primary_id → [primary_id, sibling_id, ...]`, rebuilt from ms_library stockids. Used by `/api/best-sellers` and `/api/photo-groups` to aggregate earnings across sibling IDs.
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

### Dedup — `is_already_saved` (main.py:276)
- Two-pass: (1) exact 19-char datetime match for new records, (2) `substr(date,1,10)` + price ± 0.005 fallback for legacy rows. **DO NOT add `AND LENGTH(date)=10`** — historical bug that misclassified all 19-char rows as new, causing 6,241 dups (deleted 2026-05-19).
- Index `idx_sales_dedup ON sales(stock, asset_id, date)` is required for performance.

### Adobe Stock — Pass 1 / Pass 2 contract (main.py:559+)
See `### ✅ Adobe Stock` section above. Key invariants:
- `stop_pages` early-exit in Pass 1 MUST `break`, not `return` (this killed Pass 2 silently for months).
- Pass 2 chunks: **360 days** (Adobe max is 364, 4-day safety margin).
- **Don't mark empty chunks as done.** This is what lost us $8,400 of history in 2024-2025. See Adobe section.

### Shutterstock (main.py:_shutterstock_api_collect_global)
- Browser fetch to `/api/next/v2/earnings/media_stats/day` (not direct HTTPS — DataDome blocks). Navigate to `/earnings` first to seed cookies.
- Headless mode requires `networkidle` + `add_init_script(_STEALTH_JS)`. Without them → `/unsupported-browser`.
- Header `x-end-app-name: contributor-web` is mandatory.
- Incremental dedup: last 30 days via DB MAX(date) check (saves ~92→30 daily requests). Cold-start: full 92 days.

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
- **Pass B (ms_library stockids)** — skipped when `ms_library` content fingerprint (sha1 of all `(basepath, group, sorted stockids)`) is unchanged.
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

#### Modularization (IN PROGRESS — top priority)
**Rule: move code only, NEVER rewrite logic inside functions.**
**git tag `pre-modular-20260601-112644` + `backups/main-pre-modular-*.py` = safe rollback point.**

Current state: `main.py` ~6400 lines. `utils.py` created (Step 1 done).

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

**Step 3 — collectors/ (IN PROGRESS)**
Created `collectors/` package. Files so far:
- `collectors/__init__.py` — empty
- `collectors/browser.py` ✅ — `_STEALTH_JS`, `_apply_stealth`, `_is_login_url`, `_open_browser_context`, `_do_login_flow_global`
- `collectors/adobe.py` ✅ — `_adobe_collect_direct`, `_adobe_api_collect_global`
- `collectors/shutterstock.py` — TODO
- `collectors/getty.py` — TODO
- `collectors/depositphotos.py` — TODO
- `collectors/ms_plus.py` — TODO

**Pattern for remaining collectors:**
- Import from: `sync_state`, `db`, `utils`, `collectors.browser`
- For `load_img_async` / `load_match_thumb` / `_load_browser_cookies` (still in main.py):
  use lazy import inside function body: `from main import load_img_async`
- Constants (RECIPES_DIR etc): computed from STOCK_DATA_DIR env var at module level
- Orchestrators (`_run_collector_global`, `_collect_one_stock_global`, `_sync_all_global`) stay in main.py

**Step 4 — routes (optional)**
Flask Blueprints: move `@flask_app.route(...)` functions to `routes/` or `api.py`.
Lowest priority — routes already well-documented in this CLAUDE.md.

**Step 3 — collectors/ (HIGH VALUE)**
Create `collectors/` package. One file per stock:
- `collectors/adobe.py` — `_adobe_api_collect_global`, `_adobe_collect_playwright`
- `collectors/shutterstock.py` — `_shutterstock_api_collect_global`, SS playwright fallback
- `collectors/getty.py` — `_getty_collect_direct`, `_getty_api_collect_global`
- `collectors/depositphotos.py` — `_depositphotos_collect_direct`, playwright fallback
- `collectors/ms_plus.py` — `_microstock_plus_collect_direct`, `_ms_plus_collect_global`
Each collector imports from db.py + utils.py. sync state (`_sync_stop_flag`, `_sync_log`,
`load_img_async`) passed as dependencies or imported from a shared `sync_state.py`.

**Step 3.5 — platform/ (optional, after collectors)**
Split Mac/Windows specific code:
- `platform/cookies_mac.py` — Safari binarycookies reader
- `platform/cookies_win.py` — `_decrypt_chrome_cookie_db`, `_load_appprofile_cookies_windows`
- `platform/browser.py` — stealth JS (Mac-only), channel='chrome' (Windows)

**Step 4 — routes (optional)**
Flask Blueprints: move `@flask_app.route(...)` functions to `routes/` or `api.py`.
Lowest priority — routes already well-documented in this CLAUDE.md.

**Test after each step:**
1. `python3 -c "import ast; ast.parse(open('main.py').read())"` — syntax OK
2. Start the app: `python3 main.py` → Flask starts on port 8000
3. Trigger one sync (Refresh button) — collectors run, data appears
4. Git commit with clear message: "refactor step N: ..."

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
- [ ] **Depositphotos** — inspect earnings page, find API endpoint, write `_depositphotos_api_collect`
- [ ] **Pond5** — same pattern
- [ ] **Envato** — same pattern (may need separate `envato_profile/`)

For each new stock, follow "How to add a new stock" above.
