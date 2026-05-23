<script>
  import { API_BASE } from "$lib/api.js";
  import { onMount, untrack } from 'svelte';
  import { scale, fly } from 'svelte/transition';
  import { backOut } from 'svelte/easing';
  import { RefreshCw, RotateCcw, Pencil, Trash2, Link, Plus, X, UserPlus, Merge } from 'lucide-svelte';
  import PhotoPopup from '$lib/PhotoPopup.svelte';
  import { stockColors } from '$lib/stockColors.js';
  import {
    matches, saveMatches, loadPhotoGroups, syncTick,
    toggleGroupMember, createGroup,
    groupsList, loadGroups, reloadGroups,
  } from '$lib/stores/appState.js';
  import { linkMatches } from '$lib/utils/matching.js';

  let { onGroupSelect, onGroupDeselect } = $props();

  const onToggleGroup  = toggleGroupMember;
  const onCreateGroup  = createGroup;
  const onGroupsChange = loadPhotoGroups;

  let groups       = $state(/** @type {any[]} */ ([]));
  $effect(() => { groups = $groupsList; });   // mirror central store → local reactive var

  let loading      = $state(false);
  let rebuilding   = $state(false);
  let rebuildMsg   = $state('');
  let _rebuildTimer = /** @type {ReturnType<typeof setTimeout>|null} */ (null);

  // In-modal match mode
  let modalMatchPending = $state(/** @type {any} */ (null));
  let modalMatchStatus  = $state('');
  let search       = $state('');
  let sortBy       = $state('Earnings');
  let sortDir      = $state('desc');     // 'desc' | 'asc' — toggles on repeat click
  let modalPhDir   = $state('desc');
  let displayCount = $state(30);

  /** Click a sort tab: switch field, OR if same field — toggle direction */
  function pickSort(/** @type {string} */ field) {
    if (sortBy === field) sortDir = sortDir === 'desc' ? 'asc' : 'desc';
    else { sortBy = field; sortDir = 'desc'; }
  }
  function pickModalSort(/** @type {string} */ field) {
    if (modalPhSort === field) modalPhDir = modalPhDir === 'desc' ? 'asc' : 'desc';
    else { modalPhSort = field; modalPhDir = 'desc'; }
  }
  let sentinel     = $state(/** @type {HTMLElement|null} */ (null));

  let modalGroup   = $state(/** @type {any} */ (null));
  let modalPhSort  = $state('Earnings');
  let modalDisplayCount = $state(50);            // initial render cap inside modal
  let modalSentinel = $state(/** @type {HTMLElement|null} */ (null));
  let renaming     = $state(/** @type {string|null} */ (null));
  let renameVal    = $state('');
  let confirmDel   = $state(/** @type {string|null} */ (null));
  let merging      = $state(/** @type {string|null} */ (null));  // group name being merged FROM
  let mergeTarget  = $state('');   // target group name to merge INTO
  let mergeMsg     = $state('');
  let photoPopup   = $state(/** @type {any} */ (null));

  // Add-photo dialog state
  let addPhotoOpen  = $state(false);
  let addSearch     = $state('');
  let addResults    = $state(/** @type {any[]} */ ([]));
  let addLoading    = $state(false);

  const STOCK_COLORS = $derived($stockColors);

  // Cache full photo lists per group so reopening the same modal is instant
  let _photosCache = /** @type {Record<string, any[]>} */ ({});

  async function load({ force = false } = {}) {
    loading = true;
    _photosCache = {};  // invalidate per-group photo cache
    await loadGroups({ force });
    loading = false;
  }

  /** Fetch full photos list for one group (lazy — called when modal opens). */
  async function ensureFullPhotos(/** @type {any} */ g) {
    if (!g || !g.name) return;
    if (_photosCache[g.name]) {
      g.photos = _photosCache[g.name];
      modalGroup = { ...g, photos: _photosCache[g.name] };
      return;
    }
    try {
      const r = await fetch(API_BASE + '/api/group-photos?name=' + encodeURIComponent(g.name)).then(r => r.json());
      _photosCache[g.name] = r.photos || [];
      modalGroup = { ...g, photos: _photosCache[g.name], count: r.count ?? (r.photos?.length || 0) };
    } catch (e) {
      console.error('[group-photos]', e);
    }
  }

  async function rebuild() {
    if (_rebuildTimer) { clearTimeout(_rebuildTimer); _rebuildTimer = null; }
    rebuilding = true; rebuildMsg = '';
    try {
      const resp = await fetch(API_BASE + '/api/rebuild-matches', { method: 'POST' });
      if (!resp.ok) { rebuildMsg = `Error ${resp.status}`; rebuilding = false; return; }
      const r = await resp.json();
      rebuildMsg = `✅ ${r.entries ?? 0} links · ${r.groups ?? 0} groups`;
      await load({ force: true });
      onGroupsChange?.();
    } catch (e) { rebuildMsg = `❌ ${e}`; }
    rebuilding = false;
    _rebuildTimer = setTimeout(() => rebuildMsg = '', 4000);
  }

  let filtered = $derived((() => {
    let list = search
      ? groups.filter(g => g.name.toLowerCase().includes(search.toLowerCase()))
      : [...groups];
    if (sortBy === 'Name')       list.sort((a, b) => a.name.localeCompare(b.name));
    else if (sortBy === 'Sales') list.sort((a, b) => (b.sales||0) - (a.sales||0));
    else                         list.sort((a, b) => (b.total||0) - (a.total||0));
    if (sortDir === 'asc') list.reverse();
    return list;
  })());

  let visibleGroups = $derived(filtered.slice(0, displayCount));

  let modalPhotosAll = $derived((() => {
    if (!modalGroup) return [];
    const photos = [...(modalGroup.photos || [])];
    if (modalPhSort === 'Name')   photos.sort((a, b) => (a.filename||'').localeCompare(b.filename||''));
    else if (modalPhSort === 'Sales') photos.sort((a, b) => (b.sales_count||0) - (a.sales_count||0));
    else photos.sort((a, b) => (b.earnings||0) - (a.earnings||0));
    if (modalPhDir === 'asc') photos.reverse();
    return photos;
  })());
  // Rendered slice — keeps initial DOM small when groups have hundreds of photos.
  let modalPhotos = $derived(modalPhotosAll.slice(0, modalDisplayCount));

  // Reset window when group changes or sort changes
  $effect(() => { modalGroup?.name; modalPhSort; modalDisplayCount = 50; });

  // Infinite-scroll sentinel for the modal photo list
  $effect(() => {
    if (!modalSentinel) return;
    const obs = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting && modalDisplayCount < modalPhotosAll.length) {
        modalDisplayCount = Math.min(modalDisplayCount + 50, modalPhotosAll.length);
      }
    }, { rootMargin: '300px' });
    obs.observe(modalSentinel);
    return () => obs.disconnect();
  });

  /** @param {any} ph */
  function thumbUrl(ph) {
    if (ph.thumb) return ph.thumb;
    if (ph.asset_id) return `${API_BASE}/img/cache/${ph.asset_id}`;
    return '';
  }

  /** @param {any} ph */
  function toPopupItem(ph) {
    return {
      asset_id:  ph.asset_id || '',
      by_stock:  ph.by_stock || {},
      thumb_url: ph.thumb || '',
    };
  }

  /** @param {MouseEvent} e */
  function closeModal(e) {
    if (e?.target === e?.currentTarget) { modalGroup = null; modalMatchPending = null; modalMatchStatus = ''; onGroupDeselect?.(); }
  }

  /** @param {KeyboardEvent} e */
  function handleKey(e) {
    if (e.key === 'Escape') {
      if (addPhotoOpen) { addPhotoOpen = false; addSearch = ''; addResults = []; return; }
      if (merging) { merging = null; mergeTarget = ''; mergeMsg = ''; return; }
      if (modalMatchPending) { modalMatchPending = null; modalMatchStatus = ''; return; }
      if (modalGroup) { modalGroup = null; onGroupDeselect?.(); return; }
      renaming = null; confirmDel = null; photoPopup = null;
    }
  }

  /** @param {any} ph */
  async function modalDoMatch(ph) {
    if (!modalMatchPending) {
      modalMatchPending = ph;
      modalMatchStatus = `Selected: ${ph.filename || ph.asset_id} — click another photo`;
      return;
    }
    if (ph.asset_id === modalMatchPending.asset_id) {
      modalMatchPending = null; modalMatchStatus = ''; return;
    }
    const a1 = modalMatchPending.asset_id, a2 = ph.asset_id;
    try {
      await saveMatches(linkMatches(a1, a2, $matches));
      modalMatchStatus = `✅ Linked: ${a1} ↔ ${a2}`;
      modalMatchPending = null;
    } catch (e) { modalMatchStatus = `Error: ${e}`; }
  }

  /** @param {string} name */
  async function deleteGroup(name) {
    await fetch(API_BASE + `/api/photo-groups/${encodeURIComponent(name)}`, { method: 'DELETE' });
    confirmDel = null;
    modalGroup = null;
    await load({ force: true });
    onGroupsChange?.();
  }

  async function renameGroup() {
    const newName = renameVal.trim();
    if (!newName || newName === renaming) { renaming = null; return; }
    const r = await fetch(API_BASE + '/api/photo-groups/rename', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_name: renaming, new_name: newName }),
    }).then(r => r.json());
    if (r.status === 'ok') {
      renaming = null; renameVal = '';
      await load({ force: true }); onGroupsChange?.();
    }
  }

  async function mergeGroup() {
    const target = mergeTarget.trim();
    if (!target || !merging || target === merging) return;
    const r = await fetch(API_BASE + '/api/photo-groups/merge', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: merging, target }),
    }).then(r => r.json());
    if (r.status === 'ok') {
      mergeMsg = `✅ Merged into "${target}" (${r.merged} photos)`;
      setTimeout(() => { merging = null; mergeTarget = ''; mergeMsg = ''; }, 2000);
      modalGroup = null;
      await load({ force: true }); onGroupsChange?.();
    } else {
      mergeMsg = `❌ ${r.msg || 'Error'}`;
    }
  }

  /** @param {string} groupName @param {any} ph */
  async function removeFromGroup(groupName, ph) {
    if (!ph.asset_id) return;
    if (modalGroup) {
      // Optimistic local update — modal feels instant
      const updatedPhotos = modalGroup.photos.filter((/** @type {any} */ p) => p.asset_id !== ph.asset_id);
      modalGroup = { ...modalGroup, photos: updatedPhotos, count: updatedPhotos.length };
    }
    await onToggleGroup?.(groupName, ph.asset_id);
    await load({ force: true });   // refresh group totals (preview=3 mode)
    if (modalGroup) {
      // Find the refreshed summary, then re-fetch full photos list for the modal
      const refreshed = groups.find(g => g.name === modalGroup?.name);
      if (refreshed) {
        await ensureFullPhotos(refreshed);
      } else {
        modalGroup = null;
      }
    }
  }

  /** Search DB photos to add to group */
  async function searchToAdd() {
    if (!addSearch.trim()) { addResults = []; return; }
    addLoading = true;
    try {
      const qs = new URLSearchParams({ q: addSearch, per_page: '20', page: '1', sort: 'total', period: 'All-time', stock: 'All' });
      const r = await fetch(API_BASE + `/api/sales?${qs}`).then(r => r.json());
      addResults = r.items ?? [];
    } catch { addResults = []; }
    addLoading = false;
  }

  /** @param {any} ph */
  async function addPhotoToGroup(ph) {
    if (!modalGroup) return;
    const gname = modalGroup.name;
    await onToggleGroup?.(gname, ph.asset_id);
    await load({ force: true });
    const refreshed = groups.find(g => g.name === gname);
    if (refreshed) await ensureFullPhotos(refreshed);
    addPhotoOpen = false; addSearch = ''; addResults = [];
  }

  // Reset display window when filter/sort changes
  $effect(() => {
    search; sortBy;
    displayCount = 30;
  });

  // Re-attach observer whenever the sentinel element changes (it gets recreated after each load)
  $effect(() => {
    if (!sentinel) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting) displayCount += 30;
    }, { rootMargin: '400px' });
    observer.observe(sentinel);
    return () => observer.disconnect();
  });

  // Refresh when any sync completes (force — new sales data changes group totals)
  let _lastTick = 0;
  $effect(() => {
    const t = $syncTick;
    if (t > 0 && t !== _lastTick) { _lastTick = t; untrack(() => load({ force: true })); }
  });

  // Clear pending rebuild toast timer if component unmounts mid-countdown
  $effect(() => () => { if (_rebuildTimer) clearTimeout(_rebuildTimer); });

  // First visit: load if not yet cached; subsequent tab switches: instant (store already populated)
  onMount(() => { load(); });
</script>

<svelte:window onkeydown={handleKey} />

<div class="groups">
  <!-- Header -->
  <div class="header">
    <span class="header-title">My Groups</span>
    <input class="search-input" placeholder="Search group…" bind:value={search} />
    <div style="flex:1"></div>
    <button class="action-pill {rebuilding?'busy':''}" onclick={rebuild} disabled={rebuilding}>
      <span class:spin={rebuilding}><RotateCcw size={13} strokeWidth={2} /></span>
      {rebuilding ? 'Rebuilding…' : 'Rebuild'}
    </button>
    {#if rebuildMsg}<span class="rebuild-msg">{rebuildMsg}</span>{/if}
    <button class="action-pill" onclick={load}><RefreshCw size={13} strokeWidth={2} /> Refresh</button>
  </div>

  <!-- Sort tabs -->
  <div class="sort-tabs">
    {#each ['Earnings','Sales','Name'] as tab}
      <button class="sort-tab {sortBy===tab?'active':''}" onclick={() => pickSort(tab)}>
        {tab}{sortBy===tab ? (sortDir==='desc' ? ' ↓' : ' ↑') : ''}
      </button>
    {/each}
    <span class="groups-count">{groups.length} groups</span>
  </div>

  <div class="grid scroll-y">
    {#if loading}
      <div class="loader-inline">Loading…</div>
    {:else}
      {#each visibleGroups as g, i (g.name)}
        <div class="group-card" in:fly={i < 15 ? { y: 14, duration: 180, delay: i * 16 } : { y: 0, duration: 0 }}
          role="button" tabindex="0"
          onclick={() => {
            modalGroup = g;
            modalMatchPending = null; modalMatchStatus = '';
            onGroupSelect?.(g);
            ensureFullPhotos(g);
          }}
          onkeydown={(e) => e.key === 'Enter' && (modalGroup = g, ensureFullPhotos(g))}>
          <div class="group-thumbs">
            {#each (g.photos || []).slice(0,3) as ph}
              <img src={thumbUrl(ph)} alt="" class="group-thumb"
                onerror={(e) => { const t=/** @type {HTMLImageElement} */(e.target); t.style.display='none'; t.onerror=null; }} />
            {/each}
            {#if (g.photos || []).length === 0}
              <div class="group-thumb-empty"></div>
            {/if}
          </div>
          <div class="group-info">
            <div class="group-name">{g.name}</div>
            <div class="group-meta">{g.count} photos</div>
            <div class="group-earnings">${(g.total||0).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}</div>
            <div class="group-sales dim">{(g.sales||0).toLocaleString()} sales</div>
            <div class="mini-bars">
              {#each Object.entries(g.by_stock||{}).sort(([,a],[,b])=>b.total-a.total) as [sk,d]}
                <div class="mini-seg" style="background:{STOCK_COLORS[sk]||'#888'};flex:{d.total}" title="{sk}: ${d.total.toFixed(2)}"></div>
              {/each}
            </div>
          </div>
          <!-- Card actions (visible on hover) -->
          <div class="card-actions">
            <button class="card-act" title="Rename" onclick={(e) => { e.stopPropagation(); renaming = g.name; renameVal = g.name; }}>
              <Pencil size={11} strokeWidth={2} />
            </button>
            <button class="card-act" title="Merge into another group" onclick={(e) => { e.stopPropagation(); merging = g.name; mergeTarget = ''; mergeMsg = ''; }}>
              <Merge size={11} strokeWidth={2} />
            </button>
            <button class="card-act danger" title="Delete" onclick={(e) => { e.stopPropagation(); confirmDel = g.name; }}>
              <Trash2 size={11} strokeWidth={2} />
            </button>
          </div>
        </div>
      {/each}
      {#if filtered.length === 0}
        <div class="empty">No groups found</div>
      {/if}
    {/if}
    <!-- sentinel always in DOM so the observer survives load() toggling -->
    <div class="g-sentinel" bind:this={sentinel}></div>
  </div>
</div>

<!-- ─── Confirm Delete overlay ─── -->
{#if confirmDel}
  <div class="modal-overlay" onclick={() => confirmDel = null} onkeydown={handleKey} role="dialog" tabindex="-1">
    <div class="confirm-panel" transition:scale={{ duration: 300, start: 0.88, easing: backOut }} onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="presentation">
      <div class="confirm-title">Delete group?</div>
      <div class="confirm-name">«{confirmDel}»</div>
      <div class="confirm-btns">
        <button class="btn-cancel" onclick={() => confirmDel = null}>Cancel</button>
        <button class="btn-delete" onclick={() => confirmDel && deleteGroup(confirmDel)}>Delete</button>
      </div>
    </div>
  </div>
{/if}

<!-- ─── Merge overlay ─── -->
{#if merging}
  <div class="modal-overlay" onclick={() => { merging = null; mergeTarget = ''; mergeMsg = ''; }} onkeydown={handleKey} role="dialog" tabindex="-1">
    <div class="confirm-panel" transition:scale={{ duration: 300, start: 0.88, easing: backOut }} onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="presentation">
      <div class="confirm-title">Merge «{merging}» into…</div>
      <select class="rename-input" bind:value={mergeTarget}>
        <option value="">— select target group —</option>
        {#each groups.filter(g => g.name !== merging).sort((a, b) => a.name.localeCompare(b.name)) as g}
          <option value={g.name}>{g.name}</option>
        {/each}
      </select>
      {#if mergeMsg}<div class="merge-msg">{mergeMsg}</div>{/if}
      <div class="confirm-btns">
        <button class="btn-cancel" onclick={() => { merging = null; mergeTarget = ''; mergeMsg = ''; }}>Cancel</button>
        <button class="btn-ok" disabled={!mergeTarget} onclick={mergeGroup}>Merge</button>
      </div>
    </div>
  </div>
{/if}

<!-- ─── Rename overlay ─── -->
{#if renaming}
  <div class="modal-overlay" onclick={() => renaming = null} onkeydown={handleKey} role="dialog" tabindex="-1">
    <div class="confirm-panel" transition:scale={{ duration: 300, start: 0.88, easing: backOut }} onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="presentation">
      <div class="confirm-title">Rename group</div>
      <input class="rename-input" bind:value={renameVal}
        onkeydown={(e) => e.key === 'Enter' && renameGroup()} />
      <div class="confirm-btns">
        <button class="btn-cancel" onclick={() => renaming = null}>Cancel</button>
        <button class="btn-ok" onclick={renameGroup}>OK</button>
      </div>
    </div>
  </div>
{/if}

<!-- ─── Group modal ─── -->
{#if modalGroup}
  {@const g = modalGroup}
  <div class="modal-overlay" onclick={closeModal} onkeydown={handleKey} role="dialog" tabindex="-1">
    <div class="modal-panel" transition:scale={{ duration: 320, start: 0.92, easing: backOut }} onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="presentation">

      <!-- Header -->
      <div class="modal-header">
        <span class="modal-title">{g.name}</span>
        <span class="modal-count dim">{g.count} photos</span>
        <button class="icon-act" title="Rename"
          onclick={() => { renaming = g.name; renameVal = g.name; modalGroup = null; }}><Pencil size={13} strokeWidth={1.8} /></button>
        <button class="icon-act danger" title="Delete group"
          onclick={() => { confirmDel = g.name; modalGroup = null; }}><Trash2 size={13} strokeWidth={1.8} /></button>
        <button class="close-btn" onclick={() => { modalGroup = null; modalMatchPending = null; modalMatchStatus = ''; onGroupDeselect?.(); }}><X size={15} strokeWidth={2} /></button>
      </div>

      <!-- Total -->
      <div class="modal-total">
        <span class="total-val">${(g.total||0).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
        <span class="dim">{(g.sales||0).toLocaleString()} sales</span>
      </div>

      <!-- Per-stock breakdown -->
      <div class="modal-stocks">
        {#each Object.entries(g.by_stock||{}).sort(([,a],[,b])=>b.total-a.total) as [sk,d]}
          {@const pct = g.total > 0 ? (d.total/g.total*100) : 0}
          <div class="stock-row">
            <span class="stock-icon" style="background:{STOCK_COLORS[sk]||'#555'}">
              <img src="{API_BASE}/img/stock-icon/{sk}" alt={sk}
                onerror={(e) => { const t=/** @type {HTMLImageElement} */(e.target); t.style.display='none'; }} />
            </span>
            <span class="stock-name">{sk}</span>
            <span class="stock-val">${d.total.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
            <span class="dim" style="font-size:10px">· {d.count.toLocaleString()}</span>
            <div class="bar-wrap"><div class="bar" style="width:{pct.toFixed(1)}%;background:{STOCK_COLORS[sk]||'#555'}"></div></div>
          </div>
        {/each}
      </div>

      <div class="modal-divider"></div>

      <!-- Photo sort + Add + Match buttons -->
      <div class="ph-sort-row">
        <span class="dim" style="font-size:10px">Sort:</span>
        {#each ['Earnings','Sales','Name'] as s}
          <button class="ph-sort-btn {modalPhSort===s?'active':''}" onclick={() => pickModalSort(s)}>{s}{modalPhSort===s ? (modalPhDir==='desc' ? ' ↓' : ' ↑') : ''}</button>
        {/each}
        <div style="flex:1"></div>
        <button class="match-mode-btn {modalMatchPending?'active':''}"
          onclick={() => { if (modalMatchPending) { modalMatchPending = null; modalMatchStatus = ''; } else modalMatchStatus = 'Click a photo to start matching'; }}
          title="Link photos across stocks">
          <Link size={12} strokeWidth={2} /> {modalMatchPending ? 'Cancel' : 'Match'}
        </button>
        <button class="add-photo-btn" onclick={() => { addPhotoOpen = true; addSearch=''; addResults=[]; }}>
          <UserPlus size={12} strokeWidth={2} /> Add
        </button>
      </div>

      <!-- Match status bar -->
      {#if modalMatchStatus}
        <div class="match-status-bar {modalMatchStatus.startsWith('✅')?'ok':''}">
          {modalMatchStatus}
        </div>
      {/if}

      <!-- Add-photo search panel -->
      {#if addPhotoOpen}
        <div class="add-panel" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="presentation">
          <div class="add-search-row">
            <input class="add-search-input" placeholder="Search by ID or name…"
              bind:value={addSearch}
              oninput={searchToAdd}
              onkeydown={(e) => e.key === 'Escape' && (addPhotoOpen = false)} />
            <button class="add-close-btn" onclick={() => { addPhotoOpen=false; addSearch=''; addResults=[]; }}><X size={14} strokeWidth={2} /></button>
          </div>
          <div class="add-results">
            {#if addLoading}
              <div class="add-empty dim">Searching…</div>
            {:else if addResults.length === 0 && addSearch}
              <div class="add-empty dim">Not found</div>
            {:else}
              {#each addResults as ph}
                {@const alreadyIn = (g.photos||[]).some((/** @type {any} */ p) => p.asset_id === ph.asset_id)}
                <button class="add-result-row {alreadyIn?'already-in':''}" onclick={() => !alreadyIn && addPhotoToGroup(ph)}>
                  <img src={`${API_BASE}/img/cache/${ph.asset_id}`}
                    onerror={(e) => { const t=/** @type {HTMLImageElement} */(e.target); t.src=API_BASE+'/img/placeholder'; t.onerror=null; }}
                    alt="" class="add-thumb" />
                  <div class="add-info">
                    <div class="add-id">{ph.asset_id}</div>
                    <div class="add-earn">${(ph.total||0).toFixed(2)}</div>
                  </div>
                  {#if alreadyIn}
                    <span class="already-tag">✓ In group</span>
                  {:else}
                    <span class="add-plus">+</span>
                  {/if}
                </button>
              {/each}
            {/if}
          </div>
        </div>
      {/if}

      <!-- Photo list -->
      <div class="modal-photos">
        {#each modalPhotos as ph (ph.asset_id || ph.filename)}
          {@const hasSales = (ph.earnings || 0) > 0}
          <div class="photo-row {modalMatchPending?.asset_id === ph.asset_id ? 'match-selected' : ''}"
            role="button" tabindex="0"
            onclick={() => {
              if (modalMatchStatus && !modalMatchStatus.startsWith('✅') && !modalMatchPending) {
                modalDoMatch(ph);
              } else if (modalMatchPending) {
                modalDoMatch(ph);
              } else {
                photoPopup = toPopupItem(ph);
              }
            }}
            onkeydown={(e) => e.key === 'Enter' && (photoPopup = toPopupItem(ph))}>

            <img src={thumbUrl(ph)} alt="" class="ph-thumb" loading="lazy" decoding="async"
              onerror={(e) => { const t=/** @type {HTMLImageElement} */(e.target); t.src=API_BASE+'/img/placeholder'; t.onerror=null; }} />

            <div class="ph-info">
              <div class="ph-name">{ph.filename || ph.asset_id || '—'}</div>
              <div class="ph-stocks">
                {#each Object.entries(ph.by_stock || {}).sort(([,a],[,b])=>b.total-a.total) as [sk]}
                  <span class="ph-tag" style="background:{STOCK_COLORS[sk]||'#555'}" title="{sk}">
                    <img src="{API_BASE}/img/stock-icon/{sk}" alt={sk}
                      onerror={(e) => { const t=/** @type {HTMLImageElement} */(e.target); t.style.display='none'; }} />
                  </span>
                {/each}
              </div>
            </div>

            <div class="ph-earnings">
              {#if hasSales}
                <div class="ph-earn-val">${(ph.earnings||0).toFixed(2)}</div>
                <div class="ph-earn-count">{ph.sales_count} sales</div>
                {#each Object.entries(ph.by_stock || {}).sort(([,a],[,b])=>b.total-a.total) as [sk, d]}
                  <div class="ph-earn-stock" style="color:{STOCK_COLORS[sk]||'#888'}">
                    <img src="{API_BASE}/img/stock-icon/{sk}" class="earn-icon" alt={sk}
                      onerror={(e) => { const t=/** @type {HTMLImageElement} */(e.target); t.style.display='none'; }} />
                    ${d.total.toFixed(2)} · {d.count}
                  </div>
                {/each}
              {:else}
                <span class="dim" style="font-size:10px">No sales</span>
              {/if}
            </div>

            <button class="ph-remove" title="Remove from group"
              onclick={(e) => { e.stopPropagation(); removeFromGroup(g.name, ph); }}><X size={12} strokeWidth={2} /></button>
          </div>
        {/each}
        {#if modalPhotos.length === 0}
          <div class="ph-empty dim">No photos</div>
        {/if}
        <!-- Infinite scroll sentinel -->
        {#if modalDisplayCount < modalPhotosAll.length}
          <div class="ph-sentinel" bind:this={modalSentinel}>
            <span class="dim" style="font-size:10px">Loading {modalDisplayCount}/{modalPhotosAll.length}…</span>
          </div>
        {/if}
      </div>

    </div>
  </div>
{/if}

<!-- ─── Photo popup ─── -->
{#if photoPopup}
  <PhotoPopup item={photoPopup} onClose={() => photoPopup = null} />
{/if}

<style>
  .groups { display:flex; flex-direction:column; height:100%; gap:8px; }

  .header {
    display:flex; align-items:center; gap:8px; flex-shrink:0; flex-wrap:wrap;
    background: var(--glass); backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border: 1px solid var(--glass-border); border-radius: var(--radius);
    padding: 10px 16px;
  }
  .header-title { font-size:16px; font-weight:700; color: var(--label); margin-right:4px; }
  .search-input {
    background: var(--bg3); border: 1px solid var(--glass-border); color: var(--label);
    border-radius: var(--radius-sm); padding:5px 10px; font-size:12px; width:200px;
  }
  :global(.spin) { animation: spin 1s linear infinite; display:inline-flex; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .busy { opacity:.5; cursor:not-allowed; }
  .rebuild-msg { font-size:11px; color: var(--green); }

  .sort-tabs { display:flex; align-items:center; gap:4px; flex-shrink:0; }
  .sort-tab {
    background: rgba(10,132,255,0.08); border: 1px solid rgba(10,132,255,0.35);
    color: var(--label2); font-size:12px; font-weight:600;
    padding:4px 14px; border-radius: var(--radius-pill); cursor:pointer;
    font:inherit; transition: background .15s, color .15s;
    box-shadow: 0 0 8px rgba(10,132,255,0.06);
  }
  .sort-tab.active { background: var(--sel-bg-solid); border-color: var(--sel-border); color: var(--sel-fg); box-shadow: 0 0 14px rgba(10,132,255,0.16); }
  .sort-tab:hover:not(.active) { background: rgba(10,132,255,0.14); color: var(--label); }
  .groups-count { margin-left:auto; font-size:11px; color: var(--label3); }
  .loader-inline { width:100%; color: var(--label3); font-size:12px; text-align:center; padding:40px; flex-basis:100%; }

  .grid {
    flex:1; overflow-y:auto;
    display:flex; flex-wrap:wrap;
    gap:12px; align-content:start;
  }
  .empty { width:100%; text-align:center; color: var(--label3); padding:40px; font-size:13px; }
  .g-sentinel { width:100%; height:1px; flex-basis:100%; }

  .group-card {
    cursor:pointer; display:flex; flex-direction:column;
    background: var(--glass); border: 1px solid var(--glass-border); border-radius: var(--radius);
    overflow:hidden; text-align:left; position:relative;
    width:180px; min-width:180px; max-width:180px;
    flex-shrink:0; flex-grow:0; padding:0; position:relative;
    transition: transform .18s, box-shadow .18s, border-color .18s; font:inherit; color:inherit;
    box-shadow: var(--shadow-sm), var(--glass-shine), var(--refract);
  }
  .group-card:hover { border-color: var(--accent); box-shadow: var(--shadow), var(--glass-shine), var(--refract); transform:translateY(-3px) scale(1.015); }
  .group-card:hover .card-actions { opacity:1; }

  .card-actions {
    position:absolute; top:4px; right:4px;
    display:flex; gap:3px;
    opacity:0; transition:opacity .15s;
  }
  .card-act {
    display:flex; align-items:center; justify-content:center;
    width:22px; height:22px; border-radius: var(--radius-xs);
    background: rgba(0,0,0,0.55); border: 1px solid rgba(255,255,255,0.1);
    color: var(--label2); cursor:pointer; padding:0;
    transition: background .12s, color .12s;
  }
  .card-act:hover { background: var(--accent); color:#fff; }
  .card-act.danger:hover { background: var(--red, #ff3b30); color:#fff; }
  .group-thumbs { display:flex; gap:2px; height:60px; background: var(--bg); }
  .group-thumb  { flex:1; object-fit:cover; background: var(--bg3); }
  .group-thumb-empty { flex:1; background: var(--bg3); }
  .group-info { padding:8px 10px 10px; }
  .group-name {
    font-size:12px; font-weight:600; color: var(--label);
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-bottom:2px;
  }
  .group-meta     { font-size:10px; color: var(--label3); }
  .group-earnings { font-size:14px; font-weight:700; color: var(--accent); margin-top:4px; }
  .group-sales    { font-size:10px; margin-top:1px; color: var(--label3); }
  .mini-bars { display:flex; height:3px; gap:1px; margin-top:6px; border-radius:2px; overflow:hidden; }
  .mini-seg  { height:3px; min-width:2px; }

  .modal-overlay {
    position:fixed; inset:0; z-index:300;
    backdrop-filter: blur(16px) saturate(160%); -webkit-backdrop-filter: blur(16px) saturate(160%);
    display:flex; align-items:center; justify-content:center;
  }

  .confirm-panel {
    background: var(--glass2); backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border: 1px solid var(--glass-border); border-radius: var(--radius);
    padding:20px 24px; width:320px;
    display:flex; flex-direction:column; gap:12px;
    box-shadow: var(--shadow);
  }
  .confirm-title { font-size:14px; font-weight:700; color: var(--label); }
  .confirm-name  { font-size:13px; color: var(--label2); }
  .confirm-btns  { display:flex; gap:8px; justify-content:flex-end; }
  .btn-cancel {
    background: rgba(10,132,255,0.08); border: 1px solid rgba(10,132,255,0.35); color: var(--label2);
    border-radius: var(--radius-pill); padding:6px 16px; cursor:pointer; font-size:12px; font:inherit;
  }
  .btn-delete {
    background: rgba(255,69,58,0.10); border: 1px solid rgba(255,69,58,0.42); color: var(--red);
    border-radius: var(--radius-pill); padding:6px 16px; cursor:pointer; font-size:12px; font:inherit;
  }
  .btn-delete:hover { background: rgba(255,69,58,0.18); }
  .btn-ok {
    background: var(--sel-bg-solid); border: 1px solid var(--sel-border); color: var(--sel-fg);
    border-radius: var(--radius-pill); padding:6px 16px; cursor:pointer; font-size:12px; font:inherit;
    box-shadow: 0 0 14px rgba(10,132,255,0.18);
  }
  .rename-input {
    background: var(--bg3); border: 1px solid var(--glass-border); color: var(--label);
    border-radius: var(--radius-sm); padding:8px 10px; font-size:13px; width:100%;
  }
  .merge-msg { font-size:12px; color: var(--accent); text-align:center; margin-top:2px; }

  .modal-panel {
    background: var(--glass2); backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border: 1px solid var(--glass-border); border-radius: var(--radius);
    width:560px; max-width:95vw; max-height:85vh;
    display:flex; flex-direction:column;
    box-shadow: var(--shadow), inset 0 1px 0 rgba(255,255,255,0.07); overflow:hidden;
  }
  .modal-header {
    display:flex; align-items:center; gap:8px;
    padding:14px 18px 10px; flex-shrink:0;
    border-bottom: 1px solid var(--sep);
  }
  .modal-title { font-size:15px; font-weight:700; color: var(--label); flex:1;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .modal-count { font-size:11px; color: var(--label3); }
  .icon-act {
    background: rgba(10,132,255,0.07); border: 1px solid rgba(10,132,255,0.28);
    cursor:pointer; font-size:14px; padding:4px 8px;
    border-radius: var(--radius-pill); opacity:.8; transition: background .15s, opacity .15s; font:inherit;
    display:inline-flex; align-items:center; color: var(--label2);
  }
  .icon-act:hover { opacity:1; background: rgba(10,132,255,0.15); color: var(--label); }
  .icon-act.danger:hover { background: rgba(255,69,58,0.15); border-color: rgba(255,69,58,0.4); color: var(--red); }
  .close-btn {
    background: rgba(10,132,255,0.07); border: 1px solid rgba(10,132,255,0.28);
    color: var(--label3); cursor:pointer; font-size:14px; line-height:1;
    padding:4px 9px; border-radius: var(--radius-pill); font:inherit; transition: background .15s, color .15s;
    display:inline-flex; align-items:center;
  }
  .close-btn:hover { color: var(--label); background: rgba(10,132,255,0.15); }

  .modal-total {
    display:flex; align-items:baseline; gap:10px;
    padding:10px 18px 8px; flex-shrink:0;
  }
  .total-val { font-size:24px; font-weight:700; color: var(--accent); }

  .modal-stocks { padding:0 18px 10px; display:flex; flex-direction:column; gap:8px; flex-shrink:0; }
  .stock-row { display:flex; align-items:center; gap:8px; }
  .stock-icon {
    width:22px; height:22px; border-radius: var(--radius-xs); display:inline-flex;
    align-items:center; justify-content:center;
    font-size:9px; font-weight:700; color:#fff; flex-shrink:0;
    overflow:hidden; padding:2px;
  }
  .stock-icon img { width:100%; height:100%; object-fit:contain; }
  .stock-name { font-size:12px; color: var(--label); width:100px; flex-shrink:0; }
  .stock-val  { font-size:13px; font-weight:700; color: var(--accent); min-width:80px; }
  .bar-wrap { flex:1; height:4px; background: var(--bg3); border-radius:2px; overflow:hidden; }
  .bar { height:100%; border-radius:2px; transition:width .3s; }

  .modal-divider { height:1px; background: var(--sep); flex-shrink:0; }

  .ph-sort-row {
    display:flex; align-items:center; gap:6px;
    padding:8px 16px 4px; flex-shrink:0;
  }
  .ph-sort-btn {
    background: rgba(10,132,255,0.08); border: 1px solid rgba(10,132,255,0.35);
    color: var(--label2); font-size:11px; font-weight:600;
    padding:3px 12px; border-radius: var(--radius-pill); cursor:pointer;
    font:inherit; transition: background .15s, color .15s;
  }
  .ph-sort-btn.active { background: var(--sel-bg-solid); border-color: var(--sel-border); color: var(--sel-fg); }
  .ph-sort-btn:hover:not(.active) { background: rgba(10,132,255,0.14); color: var(--label); }

  .match-mode-btn {
    background: rgba(10,132,255,0.08); border: 1px solid rgba(10,132,255,0.35); color: var(--accent);
    border-radius: var(--radius-pill); padding:4px 14px; cursor:pointer; font-size:11px; font-weight:600;
    font:inherit; flex-shrink:0; transition: background .15s, color .15s; display:inline-flex; align-items:center; gap:5px;
    box-shadow: 0 0 10px rgba(10,132,255,0.08);
  }
  .match-mode-btn:hover { background: rgba(10,132,255,0.15); color: var(--label); }
  .match-mode-btn.active {
    background: var(--sel-bg-solid); border-color: var(--sel-border); color: var(--sel-fg);
    box-shadow: 0 0 16px rgba(10,132,255,0.18);
  }

  .match-status-bar {
    font-size:11px; color: var(--accent); padding:4px 16px 2px;
    background: var(--accent-dim); flex-shrink:0;
  }
  .match-status-bar.ok { color: var(--green); background: rgba(52,199,89,0.1); }

  .photo-row.match-selected { background: var(--accent-dim); border: 1px solid var(--accent); }

  .add-photo-btn {
    background: rgba(10,132,255,0.08); border: 1px solid rgba(10,132,255,0.35); color: var(--accent);
    border-radius: var(--radius-pill); padding:4px 14px; cursor:pointer; font-size:11px; font-weight:600;
    font:inherit; flex-shrink:0; transition: background .15s; box-shadow: 0 0 10px rgba(10,132,255,0.08);
    display:inline-flex; align-items:center; gap:5px;
  }
  .add-photo-btn:hover { background: rgba(10,132,255,0.15); }

  .add-panel {
    background: var(--bg); border-top: 1px solid var(--sep); flex-shrink:0;
    max-height:200px; display:flex; flex-direction:column;
  }
  .add-search-row { display:flex; align-items:center; gap:6px; padding:8px 12px; }
  .add-search-input {
    flex:1; background: var(--bg3); border: 1px solid var(--glass-border); color: var(--label);
    border-radius: var(--radius-sm); padding:5px 10px; font-size:12px;
  }
  .add-close-btn {
    background: rgba(10,132,255,0.07); border: 1px solid rgba(10,132,255,0.28);
    color: var(--label3); cursor:pointer; font-size:14px;
    padding:4px 9px; border-radius: var(--radius-pill); font:inherit; transition: background .15s, color .15s;
    display:inline-flex; align-items:center;
  }
  .add-close-btn:hover { color: var(--label); background: rgba(10,132,255,0.15); }
  .add-results { overflow-y:auto; flex:1; }
  .add-result-row {
    display:flex; align-items:center; gap:8px; width:100%;
    background:none; border:none; padding:6px 12px;
    cursor:pointer; text-align:left; font:inherit; color: var(--label2);
    transition:background .1s;
  }
  .add-result-row:hover:not(.already-in) { background: var(--glass); }
  .add-result-row.already-in { opacity:.5; cursor:default; }
  .add-thumb { width:36px; height:36px; object-fit:cover; border-radius: var(--radius-xs); flex-shrink:0; background: var(--bg3); }
  .add-info { flex:1; min-width:0; }
  .add-id { font-size:11px; color: var(--label); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .add-earn { font-size:11px; color: var(--accent); }
  .add-plus { color: var(--green); font-size:16px; font-weight:700; flex-shrink:0; }
  .already-tag { font-size:10px; color: var(--green); flex-shrink:0; }
  .add-empty { text-align:center; padding:12px; font-size:12px; color: var(--label3); }

  .modal-photos {
    flex:1; overflow-y:auto;
    display:flex; flex-direction:column; gap:4px;
    padding:4px 12px 12px;
  }
  .photo-row {
    display:flex; align-items:center; gap:10px;
    padding:6px 8px; border-radius: var(--radius-sm); background: var(--glass);
    border: 1px solid transparent;
    cursor:pointer; transition:background .1s, border-color .1s;
  }
  .photo-row:hover { background: var(--glass2); border-color: var(--glass-border); }
  .ph-thumb {
    width:48px; height:48px; object-fit:cover; border-radius: var(--radius-xs);
    background: var(--bg3); flex-shrink:0;
  }
  .ph-info { flex:1; min-width:0; }
  .ph-name { font-size:11px; color: var(--label); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-bottom:3px; }
  .ph-stocks { display:flex; align-items:center; gap:4px; }
  .ph-tag {
    display:inline-flex; align-items:center; justify-content:center;
    width:18px; height:18px; border-radius: var(--radius-xs);
    font-size:8px; font-weight:700; color:#fff; flex-shrink:0;
    overflow:hidden; padding:2px;
  }
  .ph-tag img { width:100%; height:100%; object-fit:contain; }

  .ph-earnings { text-align:right; flex-shrink:0; min-width:80px; }
  .ph-earn-val   { font-size:13px; font-weight:700; color: var(--accent); }
  .ph-earn-count { font-size:10px; color: var(--label3); }
  .ph-earn-stock { font-size:9px; font-weight:600; display:flex; align-items:center; gap:3px; }
  .earn-icon { width:10px; height:10px; object-fit:contain; flex-shrink:0; }

  .ph-remove {
    background: rgba(255,69,58,0.08); border: 1px solid rgba(255,69,58,0.30);
    color: var(--label3); cursor:pointer;
    font-size:13px; padding:4px 8px; border-radius: var(--radius-pill); flex-shrink:0;
    transition: background .12s, color .12s; font:inherit; display:inline-flex; align-items:center;
  }
  .ph-remove:hover { color: var(--red); background: rgba(255,69,58,0.18); border-color: rgba(255,69,58,0.5); }

  .ph-empty { text-align:center; padding:20px; font-size:12px; color: var(--label3); }
  .dim { color: var(--label3); }
</style>
