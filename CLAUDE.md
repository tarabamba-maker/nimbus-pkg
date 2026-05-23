# Stock Aggregator — CLAUDE.md

## What this is

Desktop sales aggregator for photo stocks. Playwright logs into stock sites, collects sales data, saves to local SQLite DB. Flask — local HTTP server (port 8000). SvelteKit+Tauri — desktop UI.

**Flet has been fully removed.** `main.py` is now Flask-only (2144 lines). UI is `ui-tauri/` (SvelteKit + Tauri).

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
main.py              — Flask backend (API routes + Playwright collectors)
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
- **Method: API** (`_adobe_api_collect`)
- Required header: `x-requested-with: XMLHttpRequest` — without it server returns HTML not JSON
- **Do NOT use** date-range params (`start_date`, `end_date`, `time_range`) — they no longer return data
- **Correct endpoint:** `GET /en/insights/sales-earnings?limit=1000&page=N&pv={timestamp}`
- Pagination: `view.pagination` → `{limit, page, pages, total}` — iterate 1..pages
- Data in `sales.history[]` (NOT `body.history` — always empty!) → `{id, commissionAmount, thumbnailUrl, saleDate, title, originalName}`
- `saleDate` format: `"2026-05-03T01:13:34+00:00"` → slice `[:10]` = `"2026-05-03"`
- Dedup: `is_already_saved()` per record (not per month)

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
