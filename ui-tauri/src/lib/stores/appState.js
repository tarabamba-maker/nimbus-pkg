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

// ── Sync tick — incremented when any sync (downloads or browser) completes.
//    Subscribing components refresh their data when this changes.
export const syncTick = writable(0);

export function notifySyncDone() {
  syncTick.update(n => n + 1);
}

// ── Currently selected time period — shared by all tabs and stat boxes.
//    Top stat boxes drive this; Downloads/BestSellers read it and reload data.
//    Values: 'All-time' | 'Today' | 'Week' | 'Month' | 'Year'
export const currentPeriod = writable(/** @type {string} */ ('All-time'));

