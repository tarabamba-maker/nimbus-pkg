// ─────────────────────────────────────────────────────────────
// Central app state — single source of truth shared by all tabs.
// All tabs subscribe; mutations propagate automatically.
// ─────────────────────────────────────────────────────────────
import { writable, get } from 'svelte/store';
// Unified import — using one path so Vite/Rollup treats it as a single module
// (mixing $lib alias + relative path can produce duplicate chunks in prod).
import { api, API_BASE } from '$lib/api.js';

// ── Stock list (filter pills in Downloads/BestSellers/Analytics) ──
export const stockList = writable(/** @type {string[]} */ (['All']));

export async function loadStockList() {
  try {
    const list = await fetch(API_BASE + '/api/stock-list').then(r => r.json());
    stockList.set(['All', ...list]);
  } catch {}
}

// ── Cross-stock matches (used by BestSellers + Groups) ──
export const matches = writable(/** @type {Record<string,string[]>} */ ({}));

export async function loadMatches() {
  try { matches.set(await api.matches()); } catch {}
}

export async function saveMatches(/** @type {Record<string,string[]>} */ data) {
  matches.set(data);
  await api.saveMatches(data);
}

// ── Photo groups (shared across tabs) ──
export const photoGroups = writable(/** @type {Record<string,string[]>} */ ({}));

export async function loadPhotoGroups() {
  try {
    const data = await api.photoGroups();
    photoGroups.set(data || {});
    console.log('[appState] loadPhotoGroups OK:', Object.keys(data || {}).length, 'groups');
  } catch (e) {
    console.error('[appState] loadPhotoGroups FAILED:', e);
  }
}

export async function savePhotoGroups() {
  await api.savePhotoGroups(get(photoGroups));
}

/** Toggle one asset_id in/out of a group. Persists to backend. */
export async function toggleGroupMember(/** @type {string} */ name, /** @type {string} */ assetId) {
  photoGroups.update(g => {
    const next = { ...g };
    const cur = next[name] || [];
    next[name] = cur.includes(assetId) ? cur.filter(x => x !== assetId) : [...cur, assetId];
    return next;
  });
  await savePhotoGroups();
}

/** Create a new group with one initial member (or empty). */
export async function createGroup(/** @type {string} */ name, /** @type {string} */ [assetId]) {
  photoGroups.update(g => ({ ...g, [name]: assetId ? [assetId] : [] }));
  await savePhotoGroups();
}

// ── Groups list (cached across tab switches — only fetched once, then on sync) ──
export const groupsList = writable(/** @type {any[]} */ ([]));
let _groupsLoaded = false;

export async function loadGroups({ force = false } = {}) {
  if (_groupsLoaded && !force) return;
  try {
    const data = await fetch(API_BASE + '/api/groups?preview=3').then(r => r.json());
    groupsList.set(data || []);
    _groupsLoaded = true;
  } catch (e) {
    console.error('[appState] loadGroups FAILED:', e);
  }
}

/** Called after mutations (rename, delete, add photo) to refresh from backend. */
export async function reloadGroups() {
  await loadGroups({ force: true });
}

// ── App ready — set to true after waitForBackend() succeeds in +page.svelte.
//    Tabs that depend on Flask data gate their initial load on this flag.
export const appReady = writable(false);

// ── Sync tick — incremented when any sync (downloads or browser) completes.
//    Subscribing components refresh their data when this changes.
export const syncTick = writable(0);

// ── New-sale keys — Set of "asset_id|date" strings that were inserted by the
//    most recent Refresh. Survives tab switches (it's a top-level store).
//    Cleared and rewritten on each Refresh: previous "new" becomes white,
//    only sales added in the latest sync stay blue.
/** @type {import('svelte/store').Writable<Set<string>>} */
export const newSaleKeys = writable(new Set());

export function notifySyncDone() {
  // Invalidate tab caches — sync brings new data, tabs must refetch.
  _dlMem = null; _lsClear(_DL_KEY);
  _bsMem = null; _lsClear(_BS_KEY);
  _groupsLoaded = false;
  syncTick.update(n => n + 1);
}

// ── Global sync log — persists across tab switches.
//    Populated by a SINGLE app-level EventSource (in +page.svelte) so the
//    log keeps streaming regardless of which tab is open. Tabs subscribe
//    via $syncLog / $syncRunning to display state.
export const syncLog = writable(/** @type {string[]} */ ([]));
export const syncRunning = writable(false);
export const syncProgress = writable('');  // latest line shown in status pills

let _syncEs = /** @type {EventSource|null} */ (null);

/** Open SSE connection to /api/sync/stream and pump messages into stores.
 *  Idempotent — does nothing if connection already open. */
export function startSyncStream() {
  if (_syncEs) return;
  syncRunning.set(true);
  try {
    _syncEs = new EventSource(API_BASE + '/api/sync/stream');
    _syncEs.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.done) {
          stopSyncStream();
          notifySyncDone();
        } else if (d.msg) {
          syncLog.update(arr => [...arr.slice(-499), d.msg]);
          syncProgress.set(d.msg);
        }
      } catch {}
    };
    _syncEs.onerror = () => stopSyncStream();
  } catch (e) {
    console.error('[appState] startSyncStream failed', e);
    syncRunning.set(false);
  }
}

export function stopSyncStream() {
  if (_syncEs) { try { _syncEs.close(); } catch {} _syncEs = null; }
  syncRunning.set(false);
}

export function clearSyncLog() {
  syncLog.set([]);
  syncProgress.set('');
}

/** Poll /api/sync/status on app startup — if a sync is already running
 *  (e.g. user reloaded app mid-sync), open the SSE stream to resume. */
export async function resumeSyncIfRunning() {
  try {
    const s = await fetch(API_BASE + '/api/sync/status').then(r => r.json());
    if (s?.running) {
      // Pre-fill log from current state
      if (Array.isArray(s.log)) syncLog.set(s.log.slice(-500));
      if (s.progress) syncProgress.set(s.progress);
      startSyncStream();
    }
  } catch {}
}

/** Background poller — checks every 3s if a sync started elsewhere (e.g. via
 *  /api/rebuild-groups triggered from Groups tab) and auto-opens the SSE
 *  stream so the log shows up in Browser tab without manual intervention. */
let _pollTimer = /** @type {any} */ (null);
export function startSyncPoller() {
  if (_pollTimer) return;
  _pollTimer = setInterval(async () => {
    if (_syncEs) return;  // stream already open
    try {
      const s = await fetch(API_BASE + '/api/sync/status').then(r => r.json());
      if (s?.running) {
        if (Array.isArray(s.log)) syncLog.set(s.log.slice(-500));
        if (s.progress) syncProgress.set(s.progress);
        startSyncStream();
      }
    } catch {}
  }, 3000);
}

// ── Currently selected time period — shared by all tabs and stat boxes.
//    Top stat boxes drive this; Downloads/BestSellers read it and reload data.
//    Values: 'All-time' | 'Today' | 'Week' | 'Month' | 'Year'
export const currentPeriod = writable(/** @type {string} */ ('All-time'));

// ── Tab data caches — persist across tab switches (in-memory) and app restarts (localStorage).
//    Key structure: { items, totalCount, period, stock, [sort, sortDir, totalSum] }
//    Written after every successful load; read on component mount for instant display.
//    Invalidated by: sync/refresh (syncTick), or filter change (period/stock/sort).

/** @param {string} key @returns {any} */
function _lsRead(key) {
  try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch { return null; }
}
/** @param {string} key @param {any} val */
function _lsWrite(key, val) {
  try { localStorage.setItem(key, JSON.stringify(val)); } catch {}
}
/** @param {string} key */
function _lsClear(key) {
  try { localStorage.removeItem(key); } catch {}
}

// Downloads cache
const _DL_KEY = 'dl_cache_v1';
/** @type {{ items:any[], totalCount:number, period:string, stock:string } | null} */
let _dlMem = null;   // in-memory (survives tab switch, cleared on refresh)

export const downloadsCache = {
  /** @returns {{ items:any[], totalCount:number, period:string, stock:string } | null} */
  read() { return _dlMem ?? _lsRead(_DL_KEY); },
  /** @param {{ items:any[], totalCount:number, period:string, stock:string }} d */
  write(d) { _dlMem = d; _lsWrite(_DL_KEY, d); },
  clear() { _dlMem = null; _lsClear(_DL_KEY); },
};

// BestSellers cache
const _BS_KEY = 'bs_cache_v1';
/** @type {{ items:any[], totalCount:number, totalSum:number, period:string, stock:string, sort:string, sortDir:string } | null} */
let _bsMem = null;

export const bestSellersCache = {
  /** @returns {{ items:any[], totalCount:number, totalSum:number, period:string, stock:string, sort:string, sortDir:string } | null} */
  read() { return _bsMem ?? _lsRead(_BS_KEY); },
  /** @param {{ items:any[], totalCount:number, totalSum:number, period:string, stock:string, sort:string, sortDir:string }} d */
  write(d) { _bsMem = d; _lsWrite(_BS_KEY, d); },
  clear() { _bsMem = null; _lsClear(_BS_KEY); },
};

