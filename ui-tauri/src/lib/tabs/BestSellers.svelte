<script>
  import { API_BASE } from "$lib/api.js";
  import { untrack } from 'svelte';
  import { fly } from 'svelte/transition';
  import { Link, Users } from 'lucide-svelte';
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
  let filtersH   = $state(56);
  let totalCount = $state(0);
  let totalSum   = $state(0);
  let loading    = $state(false);
  let search     = $state('');

  let popup   = $state(/** @type {any} */ (null));
  let ctxMenu = $state(/** @type {{item:any, x:number, y:number}|null} */ (null));
  let sentinel = $state(/** @type {HTMLElement|null} */ (null));

  // Ungrouped filter
  let ungroupedOnly = $state(false);

  // Multi-select (Ctrl+click)
  let selected = $state(/** @type {Set<string>} */ (new Set()));

  function toggleSelect(e, item) {
    if (!e.ctrlKey && !e.metaKey) return false;
    e.stopPropagation();
    const s = new Set(selected);
    if (s.has(item.asset_id)) s.delete(item.asset_id);
    else s.add(item.asset_id);
    selected = s;
    return true;
  }

  function clearSelection() { selected = new Set(); }

  async function addSelectedToGroup(groupName) {
    const ids = [...selected];
    const pg = { ...$photoGroups };
    if (!pg[groupName]) pg[groupName] = [];
    for (const id of ids) {
      if (!pg[groupName].includes(id)) pg[groupName].push(id);
    }
    await fetch(API_BASE + '/api/photo-groups', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(pg) });
    photoGroups.set(pg);
    clearSelection();
  }

  let newGroupName = $state('');
  let showNewGroupInput = $state(false);
  let groupSearch = $state('');
  let showGroupDropdown = $state(false);
  let groupDropdownPos = $state({ top: 0, left: 0 });
  /** @param {FocusEvent|InputEvent} e */
  function openGroupDropdown(e) {
    const rect = /** @type {HTMLElement} */ (e.target).getBoundingClientRect();
    groupDropdownPos = { top: rect.bottom + 4, left: rect.left };
    showGroupDropdown = true;
  }

  const filteredGroups = $derived(
    Object.keys($photoGroups).sort().filter(g =>
      !groupSearch || g.toLowerCase().includes(groupSearch.toLowerCase())
    )
  );

  async function createGroupFromSelected() {
    if (!newGroupName.trim()) return;
    await addSelectedToGroup(newGroupName.trim());
    newGroupName = '';
    showNewGroupInput = false;
  }

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

  async function loadMore() {
    if (loading || items.length >= totalCount) return;
    page++;
    await load();
    // After load, if sentinel is still in viewport (content doesn't fill screen),
    // keep loading — same behavior with or without ungroupedOnly filter.
    if (items.length < totalCount && sentinel) {
      const rect = sentinel.getBoundingClientRect();
      if (rect.top < window.innerHeight + 800) loadMore();
    }
  }

  /** @param {MouseEvent} e @param {any} item */
  function handleCardClick(e, item) {
    e.stopPropagation();
    if (toggleSelect(e, item)) return;
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
    // If this item is part of a multi-selection, pass all selected items
    if (selected.size > 1 && selected.has(item.asset_id)) {
      const selItems = items.filter(it => selected.has(it.asset_id));
      ctxMenu = { item, items: selItems, x: e.clientX, y: e.clientY };
    } else {
      ctxMenu = { item, x: e.clientX, y: e.clientY };
    }
  }

  $effect(() => {
    if (!sentinel) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting) loadMore();
    }, { rootMargin: '800px' });
    observer.observe(sentinel);
    // If content doesn't fill viewport on mount, trigger load immediately
    setTimeout(() => { if (!loading && items.length < totalCount) loadMore(); }, 100);
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

<div class="bs glass-wrap">
  <!-- Floating frosted panel: header + select-bar (content scrolls under it) -->
  <div class="bs-top glass-panel" bind:clientHeight={filtersH}>
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
      <button class="pill {ungroupedOnly ? 'pill-active' : ''}"
        onclick={() => { ungroupedOnly = !ungroupedOnly; clearSelection(); }}
        title="Show only photos not in any group">
        <Users size={11} strokeWidth={2} /> Ungrouped
      </button>
    </div>
  </div>

  <!-- Multi-select action bar -->
  {#if selected.size > 0}
    <div class="select-bar">
      <span class="select-count">{selected.size} selected</span>
      <div style="flex:1"></div>
      {#if showNewGroupInput}
        <input class="new-group-input" placeholder="Group name…" bind:value={newGroupName}
          onkeydown={(e) => { if (e.key==='Enter') createGroupFromSelected(); if (e.key==='Escape') showNewGroupInput=false; }}
          autofocus />
        <button class="sel-btn sel-btn-ok" onclick={createGroupFromSelected}>Create</button>
        <button class="sel-btn" onclick={() => showNewGroupInput=false}>✕</button>
      {:else}
        <div class="sel-group-wrap">
          <span class="sel-label">Add to group:</span>
          <div class="sel-search-wrap">
            <input class="sel-search" placeholder="Search group…"
              bind:value={groupSearch}
              onfocus={openGroupDropdown}
              onblur={() => setTimeout(() => showGroupDropdown = false, 150)}
              oninput={openGroupDropdown} />
          </div>
        </div>
        <button class="sel-btn sel-btn-new" onclick={() => showNewGroupInput = true}>+ New group</button>
        <button class="sel-btn" onclick={clearSelection}>✕ Clear</button>
      {/if}
    </div>
  {/if}
  </div><!-- /.bs-top -->

  <!-- Grid: fixed 160px cards (scrolls under the frosted panel) -->
  <div class="grid glass-canvas" style="--panel-h:{filtersH}px">
    {#each items as item, i (item.asset_id)}
      {@const isSrc      = pendingMatch === item.asset_id}
      {@const stockKeys  = Object.keys(item.by_stock || {}).length ? Object.keys(item.by_stock) : [item.stock]}
      {@const inGroups   = Object.entries($photoGroups).filter(([,ids]) => ids.includes(item.asset_id)).map(([n]) => n)}
      {@const isSelected = selected.has(item.asset_id)}
      {#if !ungroupedOnly || inGroups.length === 0}
      <div class="card {isSrc ? 'match-src' : ''} {pendingMatch && !isSrc ? 'match-pick' : ''} {isSelected ? 'card-selected' : ''}"
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
            src={`${API_BASE}/img/cache/${item.thumb_aid || item.asset_id}`}
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
      {/if}
    {/each}
    {#if loading}
      <div class="loader">Loading…</div>
    {/if}
    <div class="sentinel" bind:this={sentinel}></div>
  </div>
</div>

<!-- Group search dropdown portal (outside stacking context of select-bar) -->
{#if showGroupDropdown && filteredGroups.length > 0}
  <div class="sel-dropdown-portal"
    style="top:{groupDropdownPos.top}px;left:{groupDropdownPos.left}px">
    {#each filteredGroups.slice(0, 12) as g}
      <button class="sel-drop-item"
        onmousedown={() => { addSelectedToGroup(g); groupSearch = ''; showGroupDropdown = false; }}>
        {g}
      </button>
    {/each}
  </div>
{/if}

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
  /* layout (.glass-wrap/.glass-panel/.glass-canvas) is global — see +page.svelte */
  .bs-top { display:flex; flex-direction:column; gap:6px; }
  .header { display:flex; flex-direction:column; gap:6px; }
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

  .pill {
    display:inline-flex; align-items:center; gap:4px;
    padding:3px 10px; border-radius: var(--radius-pill); border:1px solid var(--glass-border);
    background: var(--bg2); color: var(--label2); font-size:11px; cursor:pointer;
    transition: background 0.15s, color 0.15s;
  }
  .pill:hover { background: var(--bg3); color: var(--label); }
  .pill-active { background: rgba(10,132,255,0.15); border-color: rgba(10,132,255,0.4); color: var(--accent); }

  .select-bar {
    display:flex; align-items:center; gap:8px; flex-shrink:0;
    background: rgba(10,132,255,0.12); border:1px solid rgba(10,132,255,0.30);
    border-radius: var(--radius); padding:7px 12px;
  }
  .select-count { font-size:12px; font-weight:700; color: var(--accent); }
  .sel-label { font-size:11px; color: var(--label2); }
  .sel-group-wrap { display:flex; align-items:center; gap:6px; }
  .sel-select {
    font-size:11px; padding:3px 6px; border-radius:6px;
    border:1px solid var(--glass-border); background: var(--bg2); color: var(--label);
    max-width:200px;
  }
  .sel-btn {
    font-size:11px; padding:3px 10px; border-radius: var(--radius-pill);
    border:1px solid var(--glass-border); background: var(--bg2); color: var(--label2);
    cursor:pointer; white-space:nowrap;
  }
  .sel-btn:hover { background: var(--bg3); color: var(--label); }
  .sel-btn-new { color: var(--accent); border-color: rgba(10,132,255,0.4); }
  .sel-btn-ok  { background: var(--accent); color:#fff; border-color: var(--accent); }
  .new-group-input {
    font-size:12px; padding:3px 8px; border-radius:6px;
    border:1px solid rgba(10,132,255,0.4); background: var(--bg2); color: var(--label);
    width:160px; outline:none;
  }

  .sel-search-wrap { position:relative; }
  .sel-search {
    font-size:12px; padding:3px 8px; border-radius:6px; width:160px;
    border:1px solid var(--glass-border); background: var(--bg2); color: var(--label);
    outline:none;
  }
  .sel-dropdown-portal {
    position:fixed; z-index:9000;
    background: var(--glass2); border:1px solid var(--glass-border);
    border-radius: var(--radius); backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    width:220px; max-height:200px; overflow-y:auto;
    box-shadow: var(--shadow);
  }
  .sel-drop-item {
    display:block; width:100%; text-align:left; padding:6px 12px;
    background:none; border:none; color: var(--label2); font-size:12px; cursor:pointer;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-family:inherit;
  }
  .sel-drop-item:hover { background: rgba(255,255,255,0.06); color: var(--label); }

  .card-selected {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .grid {
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
