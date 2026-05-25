<script>
  import { API_BASE } from "$lib/api.js";
  import { untrack } from 'svelte';
  import { fly } from 'svelte/transition';
  import { Link } from 'lucide-svelte';
  import PhotoPopup from '$lib/PhotoPopup.svelte';
  import GroupContextMenu from '$lib/components/GroupContextMenu.svelte';
  import FilterPills from '$lib/components/FilterPills.svelte';
  import { stockColors } from '$lib/stockColors.js';
  import { get } from 'svelte/store';
  import {
    photoGroups, stockList, matches, loadMatches, saveMatches, syncTick, currentPeriod, appReady, bestSellersCache,
  } from '$lib/stores/appState.js';
  import { unlinkFromMatches, linkMatches, getSiblings } from '$lib/utils/matching.js';

  let { onStockChange } = $props();

  let sort     = $state('Earnings');
  let sortDir  = $state('desc');  // 'desc' | 'asc' — toggles when same sort field re-clicked

  /** @param {string} s */
  function pickSort(s) {
    if (s === sort) sortDir = sortDir === 'desc' ? 'asc' : 'desc';
    else { sort = s; sortDir = 'desc'; }
    load(true);
  }
  let stock    = $state('All');
  const perPage = 50;
  let page     = $state(1);

  let items      = $state(/** @type {any[]} */ ([]));
  let totalCount = $state(0);
  let totalSum   = $state(0);
  let loading    = $state(false);
  let search     = $state('');

  let popup   = $state(/** @type {any} */ (null));
  let ctxMenu = $state(/** @type {{item:any, x:number, y:number}|null} */ (null));
  let sentinel = $state(/** @type {HTMLElement|null} */ (null));

  // Match mode — pendingMatch != null means mode is active (source selected)
  let pendingMatch = $state(/** @type {string|null} */ (null));  // asset_id of source

  // Auto-match
  let autoMatching  = $state(false);
  let autoMatchMsg  = $state('');
  let _autoMatchTimer = /** @type {ReturnType<typeof setTimeout>|null} */ (null);

  async function autoMatch() {
    if (_autoMatchTimer) { clearTimeout(_autoMatchTimer); _autoMatchTimer = null; }
    autoMatching = true; autoMatchMsg = '';
    try {
      const r = await fetch(API_BASE + '/api/rebuild-matches', { method: 'POST' }).then(r => r.json());
      autoMatchMsg = `✅ ${r.entries ?? 0} links`;
      await loadMatches();
      load(true);
    } catch (e) { autoMatchMsg = `❌ ${e}`; }
    autoMatching = false;
    _autoMatchTimer = setTimeout(() => autoMatchMsg = '', 4000);
  }

  const STOCK_COLORS = $derived($stockColors);

  /** @param {string} aid */
  async function unlinkMatch(aid) {
    await saveMatches(unlinkFromMatches(aid, $matches));
    popup = null;
    load(true);
  }

  async function load(reset = false, skipCache = false) {
    if (loading) return;  // guard against concurrent loads (period + syncTick effects)
    const _period = get(currentPeriod);

    // Restore from cache on tab switch / app restart
    if (reset && !skipCache && !search) {
      const cached = bestSellersCache.read();
      if (cached && cached.period === _period && cached.stock === stock &&
          cached.sort === sort && cached.sortDir === sortDir) {
        items = cached.items; totalCount = cached.totalCount; totalSum = cached.totalSum;
        page = Math.ceil(cached.items.length / perPage) || 1;
        return;
      }
    }

    if (reset) { page = 1; items = []; totalCount = 0; totalSum = 0; }
    loading = true;
    try {
      const sortParam = sort === 'Earnings' ? 'total' : 'count';
      const qs = new URLSearchParams({ period: _period, stock, page: String(page), per_page: String(perPage), sort: sortParam, dir: sortDir, q: search });
      const r  = await fetch(API_BASE + `/api/sales?${qs}`).then(r => r.json());
      const incoming = r.items ?? [];
      items      = reset ? incoming : [...items, ...incoming];
      totalCount = r.total_count ?? 0;
      totalSum   = r.total_sum   ?? 0;
      // Persist to cache (only on full reset, not append-page)
      if (reset) {
        bestSellersCache.write({ items, totalCount, totalSum, period: _period, stock, sort, sortDir });
      }
    } catch (e) { console.error(e); }
    loading = false;
  }

  function loadMore() {
    if (loading || items.length >= totalCount) return;
    page++;
    load();
  }

  /** @param {Event} e @param {any} item */
  function handleCardClick(e, item) {
    e.stopPropagation();
    if (pendingMatch) { doMatch(item); return; }
    popup = item;
  }

  /** Called from popup "Match" button — enter match mode with this item pre-selected */
  /** @param {any} item */
  function startMatchFrom(item) {
    popup = null;
    pendingMatch = item.asset_id;
  }

  /** @param {any} item */
  async function doMatch(item) {
    if (!pendingMatch) { pendingMatch = item.asset_id; return; }
    if (item.asset_id === pendingMatch) { pendingMatch = null; return; }
    const a1 = pendingMatch, a2 = item.asset_id;
    pendingMatch = null;
    try {
      await saveMatches(linkMatches(a1, a2, $matches));
      load(true);
    } catch (err) { console.error('match error', err); }
  }

  /** @param {MouseEvent} e @param {any} item */
  function openCtx(e, item) {
    e.preventDefault();
    ctxMenu = { item, x: e.clientX, y: e.clientY };
  }

  $effect(() => {
    if (!sentinel) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting) loadMore();
    }, { rootMargin: '400px' });
    observer.observe(sentinel);
    return () => observer.disconnect();
  });

  // Refresh when any sync completes — skip cache
  let _lastTick = get(syncTick);
  $effect(() => {
    const t = $syncTick;
    if (t > 0 && t !== _lastTick) { _lastTick = t; untrack(() => load(true, true)); }
  });

  // Reload when period changes OR when backend becomes ready.
  $effect(() => {
    const period = $currentPeriod;
    const ready  = $appReady;
    if (!ready) return;
    untrack(() => load(true));
  });

  // Clear pending auto-match toast timer if component unmounts mid-countdown
  $effect(() => () => { if (_autoMatchTimer) clearTimeout(_autoMatchTimer); });
</script>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape' && pendingMatch) pendingMatch = null; }} />

<div class="bs">
  <!-- Header -->
  <div class="header">
    <div class="top-row">
      <span class="total-sum">${totalSum.toFixed(2)}</span>
      <span class="total-count">{totalCount} photos</span>
      <div style="flex:1"></div>
      {#if pendingMatch}
        <div class="match-mode-bar">
          <Link size={13} strokeWidth={1.8} />
          Linking «{pendingMatch}» — click another photo
          <button class="match-cancel" onclick={() => pendingMatch = null}>✕ Cancel</button>
        </div>
      {/if}
      <button class="action-pill {autoMatching?'busy':''}" onclick={autoMatch} disabled={autoMatching}>
        <Link size={13} strokeWidth={1.8} /> {autoMatching ? 'Matching…' : 'Auto-match'}
      </button>
      {#if autoMatchMsg}<span class="automatch-msg">{autoMatchMsg}</span>{/if}
      <input class="search-input" placeholder="Search ID…" bind:value={search} oninput={() => load(true)} />
    </div>
    <div class="filter-row">
      <FilterPills label="Sort:" options={['Earnings','Sales']}
        value={sort} activeSuffix={sortDir==='desc' ? ' ↓' : ' ↑'}
        onSelect={pickSort} />
      <FilterPills label="Stock:" options={$stockList}
        value={stock} onSelect={(s) => { stock=s; load(true); onStockChange?.(s); }} />
    </div>
  </div>

  <!-- Grid: fixed 160px cards -->
  <div class="grid scroll-y">
    {#each items as item, i (item.asset_id)}
      {@const isSrc      = pendingMatch === item.asset_id}
      {@const stockKeys  = Object.keys(item.by_stock || {}).length ? Object.keys(item.by_stock) : [item.stock]}
      {@const inGroups   = Object.entries($photoGroups).filter(([,ids]) => ids.includes(item.asset_id)).map(([n]) => n)}
      <div class="card {isSrc ? 'match-src' : ''} {pendingMatch && !isSrc ? 'match-pick' : ''}"
        in:fly={i < 15 ? { y: 14, duration: 180, delay: i * 16 } : { y: 0, duration: 0 }}
        onclick={(e) => handleCardClick(e, item)}
        oncontextmenu={(e) => openCtx(e, item)}
        role="button" tabindex="0"
        onkeydown={(e) => e.key === 'Enter' && handleCardClick(e, item)}>

        <!-- stock color bar -->
        <div class="stock-bars">
          {#each stockKeys as sk}
            <div class="stock-bar" style="background:{STOCK_COLORS[sk]||'#888'};flex:1" title={sk}></div>
          {/each}
        </div>

        <!-- Thumbnail -->
        <div class="img-box">
          <img
            src={`${API_BASE}/img/cache/${item.asset_id}`}
            onerror={(e) => {
              const t = /** @type {HTMLImageElement} */ (e.target), url = item.thumb_url || '';
              if (url && t.src !== url) { t.src = url; }
              else { t.src = API_BASE + '/img/placeholder'; t.onerror = null; }
            }}
            alt="" class="thumb" />
        </div>

        <!-- Info -->
        <div class="meta">
          <div class="card-id">ID: {item.asset_id}</div>
          <div class="card-price">${item.total.toFixed(2)}</div>
          <div class="card-meta">
            {stockKeys.length > 1 ? stockKeys.length + ' stocks' : (stockKeys[0] ?? '')} · {item.count ?? 0} sales
          </div>
          {#if inGroups.length > 0}
            <div class="group-badge">📁 {inGroups[0]}</div>
          {/if}
        </div>
        <!-- Link icon: appears on hover, enters match mode for this card -->
        <div class="card-actions">
          <button class="card-act" title="Link to another photo" onclick={(e) => { e.stopPropagation(); pendingMatch = item.asset_id; }}>
            <Link size={11} strokeWidth={2} />
          </button>
        </div>
      </div>
    {/each}
    {#if loading}
      <div class="loader">Loading…</div>
    {/if}
    <div class="sentinel" bind:this={sentinel}></div>
  </div>
</div>

<!-- Context menu (right-click → group management) -->
{#if ctxMenu}
  <GroupContextMenu ctx={ctxMenu} onClose={() => ctxMenu = null}
    onOpenDetails={(item) => popup = item} />
{/if}

<!-- Popup (left-click) -->
{#if popup}
  <PhotoPopup
    item={popup}
    matchedIds={getSiblings(popup.asset_id, $matches) || []}
    onClose={() => popup = null}
    onUnlink={() => unlinkMatch(popup?.asset_id)}
    onMatch={() => { const it = popup; popup = null; if (it) startMatchFrom(it); }} />
{/if}

<style>
  .bs { display:flex; flex-direction:column; height:100%; gap:8px; }

  .header {
    display:flex; flex-direction:column; gap:6px; flex-shrink:0;
    background: var(--glass); border:1px solid var(--glass-border);
    backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border-radius: var(--radius); padding:10px 12px;
    box-shadow: var(--shadow-sm), var(--glass-shine);
  }
  .top-row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .total-sum { font-size:22px; font-weight:700; color: var(--accent); letter-spacing:-0.5px; }
  .total-count { font-size:11px; color: var(--label2); }

  .match-mode-bar {
    display:flex; align-items:center; gap:6px;
    background: rgba(10,132,255,0.10); border:1px solid rgba(10,132,255,0.30);
    border-radius: var(--radius-pill); padding:4px 12px;
    font-size:12px; color: var(--accent); font-weight:600;
  }
  .match-cancel {
    background:none; border:none; color: var(--label2); cursor:pointer; font-size:12px;
    padding:0 2px; margin-left:4px; font-family:inherit;
  }
  .match-cancel:hover { color: var(--label); }

  .busy { opacity: .45; cursor: not-allowed; }
  .automatch-msg { font-size:11px; color: var(--green); flex-shrink:0; }

  .filter-row { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }

  .grid {
    flex:1; overflow-y:auto;
    display:flex; flex-wrap:wrap;
    gap:8px; align-content:start; padding-right:2px;
  }

  .card {
    width:160px; flex-shrink:0; flex-grow:0;
    display:flex; flex-direction:column;
    background: var(--glass); border-radius: var(--radius);
    border:1px solid var(--glass-border); overflow:hidden; cursor:pointer; position:relative;
    transition: transform .18s, box-shadow .18s, border-color .18s;
    box-shadow: var(--shadow-sm), var(--glass-shine), var(--refract);
  }
  .card:hover  { border-color: var(--accent); transform:translateY(-3px) scale(1.015); box-shadow: var(--shadow), var(--glass-shine), var(--refract); }
  .card:hover .card-actions { opacity:1; }
  .card.match-src  { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent), var(--shadow-sm); cursor:default; }
  .card.match-src:hover { transform:none; }
  .card.match-pick { cursor:crosshair; }
  .card.match-pick:hover { border-color: var(--green, #30d158); box-shadow: 0 0 0 1px var(--green, #30d158), var(--shadow); }

  .card-actions {
    position:absolute; top:4px; right:4px;
    display:flex; gap:3px; opacity:0; transition:opacity .15s;
  }
  .card-act {
    display:flex; align-items:center; justify-content:center;
    width:22px; height:22px; border-radius: var(--radius-xs, 4px);
    background: rgba(0,0,0,0.55); border: 1px solid rgba(255,255,255,0.1);
    color: var(--label2); cursor:pointer; padding:0;
    transition: background .12s, color .12s;
  }
  .card-act:hover { background: var(--accent); color:#fff; }

  .stock-bars { display:flex; height:3px; gap:1px; flex-shrink:0; }
  .stock-bar  { height:3px; min-width:4px; }

  .img-box { width:160px; height:120px; background: var(--bg3); flex-shrink:0; overflow:hidden; }
  .thumb   { width:100%; height:100%; object-fit:cover; display:block; }

  .meta      { padding:7px 9px 9px; }
  .card-id   { font-size:9px; color: var(--label3); margin-bottom:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .card-price { font-size:16px; font-weight:700; color: var(--accent); line-height:1.2; letter-spacing:-0.3px; }
  .card-meta  { font-size:10px; color: var(--label2); margin-top:2px; }
  .group-badge { font-size:9px; color: var(--accent); margin-top:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; opacity:.7; }

  .sentinel { width:100%; height:1px; flex-basis:100%; }
  .loader { width:100%; text-align:center; color: var(--label3); padding:20px; flex-basis:100%; }

</style>
