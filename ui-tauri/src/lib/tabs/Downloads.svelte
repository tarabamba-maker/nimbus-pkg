<script>
  import { API_BASE } from "$lib/api.js";
  import { untrack } from 'svelte';
  import { fly } from 'svelte/transition';
  import { RefreshCw } from 'lucide-svelte';
  import PhotoPopup from '$lib/PhotoPopup.svelte';
  import GroupContextMenu from '$lib/components/GroupContextMenu.svelte';
  import FilterPills from '$lib/components/FilterPills.svelte';
  import { stockColors } from '$lib/stockColors.js';
  import { get } from 'svelte/store';
  import { photoGroups, stockList, loadStockList, notifySyncDone, syncTick, currentPeriod, appReady, downloadsCache } from '$lib/stores/appState.js';

  let { onRefresh, onStockChange } = $props();

  let stock   = $state('All');
  let perPage = $state(50);
  let page    = $state(1);

  let items      = $state(/** @type {any[]} */ ([]));
  let totalCount = $state(0);
  let loading    = $state(false);
  let syncing    = $state(false);
  let syncLog    = $state('');
  let stocks     = $state(['All']);
  let lastSync   = $state('');

  let popup   = $state(/** @type {any} */ (null));
  let ctxMenu = $state(/** @type {{item:any, x:number, y:number}|null} */ (null));

  let sentinel = /** @type {HTMLElement|null} */ (null);

  const STOCK_COLORS = $derived($stockColors);

  // mirror central stockList store
  $effect(() => { stocks = $stockList; });

  /**
   * @param {boolean} [reset]
   * @param {number|null} [prevMaxId] - if set, items with id > prevMaxId are marked _isNew
   * @param {boolean} [skipCache] - if true, always fetch (used after sync)
   */
  async function load(reset = false, prevMaxId = null, skipCache = false) {
    if (loading) return;
    const _period  = get(currentPeriod);
    const _stock   = stock;

    // Restore from cache on first load (reset=true, no new-item highlighting)
    if (reset && prevMaxId === null && !skipCache) {
      const cached = downloadsCache.read();
      if (cached && cached.period === _period && cached.stock === _stock) {
        items = cached.items;
        totalCount = cached.totalCount;
        page = Math.ceil(cached.items.length / perPage) || 1;
        return;  // instant — no fetch needed
      }
    }

    if (reset) { page = 1; items = []; }
    loading = true;
    const _page    = page;
    const _perPage = perPage;
    try {
      const qs = `period=${_period}&stock=${encodeURIComponent(_stock)}&page=${_page}&per_page=${_perPage}`;
      const d  = await fetch(API_BASE + `/api/feed?${qs}`).then(r => r.json());
      const incoming = d.items ?? [];
      if (reset) {
        items = incoming.map((/** @type {any} */ it) => ({
          ...it,
          _isNew: prevMaxId !== null && (it.id || 0) > prevMaxId,
        }));
      } else {
        items = [...items, ...incoming.map((/** @type {any} */ it) => ({ ...it, _isNew: false }))];
      }
      totalCount = d.total_count ?? 0;
      lastSync = new Date().toLocaleString('uk', {
        day: 'numeric', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit'
      });
      // Persist to cache. Preserve _isNew when this is a post-sync load (prevMaxId set)
      // so blues survive tab switches. Strip them on plain loads (no prevMaxId).
      if (reset || page === 1) {
        downloadsCache.write({
          items: items.map(it => ({ ...it, _isNew: prevMaxId !== null ? it._isNew : false })),
          totalCount, period: _period, stock: _stock,
        });
      }
    } catch (e) { console.error(e); }
    loading = false;
  }

  function loadMore() {
    if (loading || items.length >= totalCount) return;
    page++;
    load();
  }

  /** @param {any} item */
  function openPopup(item) { popup = item; }

  /**
   * Refresh: start sync → on done reload with new-item highlighting.
   * prevMaxId captured NOW (before sync) so any item inserted during sync is marked blue.
   */
  let _syncEs = /** @type {EventSource|null} */ (null);
  let _lastTick = get(syncTick);  // init to current tick so remount doesn't re-trigger load

  async function doRefresh() {
    if (syncing) return;
    syncing = true;
    syncLog = 'Starting sync…';
    const prevMaxId = items.length ? Math.max(...items.map((/** @type {any} */ it) => it.id || 0)) : 0;

    const finish = async () => {
      if (_syncEs) { _syncEs.close(); _syncEs = null; }
      await loadStockList();
      await load(true, prevMaxId);  // marks newly-arrived items _isNew (blue)
      // AWAIT rebuild-matches — running it in parallel with other tabs' reloads
      // hammers SQLite for ~80s and freezes Groups/BestSellers. Better to delay
      // notifySyncDone() until Flask is free.
      syncLog = 'Rebuilding matches…';
      try { await fetch(API_BASE + '/api/rebuild-matches', { method: 'POST' }); }
      catch {}
      onRefresh?.();
      // Pre-bump _lastTick so the syncTick $effect below won't re-run load() and wipe the highlights.
      _lastTick++;
      notifySyncDone();
      syncing = false;
      syncLog = '';
    };

    try {
      const r = await fetch(API_BASE + '/api/sync/start', { method: 'POST' }).then(r => r.json()).catch(() => ({ ok: false }));
      if (r.ok === false && r.msg) {
        syncLog = r.msg || 'Already running';
        await finish();
        return;
      }
      _syncEs = new EventSource(API_BASE + '/api/sync/stream');
      _syncEs.onmessage = async (e) => {
        const d = JSON.parse(e.data);
        if (d.done) { await finish(); }
        else { syncLog = d.msg || ''; }
      };
      _syncEs.onerror = async () => { await finish(); };
    } catch {
      await finish();
    }
  }

  $effect(() => () => { if (_syncEs) { _syncEs.close(); _syncEs = null; } });

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

  // Refresh when any sync (e.g. from Browser tab) completes — skip cache, data is stale
  $effect(() => {
    const t = $syncTick;
    if (t > 0 && t !== _lastTick) { _lastTick = t; untrack(() => load(true, null, true)); }
  });

  // Reload when period changes OR when backend becomes ready.
  // Gated on $appReady so the initial mount in .app doesn't fire before Flask starts.
  $effect(() => {
    const period = $currentPeriod;
    const ready  = $appReady;
    if (!ready) return;
    untrack(() => load(true, null));
  });
</script>

<div class="tab-wrap">
  <!-- Filters: period is controlled by top stat boxes; only Stock + Refresh here -->
  <div class="filters">
    <div class="filter-row">
      <FilterPills label="Stock:" options={stocks}
        value={stock} onSelect={(s) => { stock=s; load(true, null); onStockChange?.(s); }} />
      <button class="action-pill refresh-btn {syncing?'busy':''}" onclick={doRefresh} disabled={syncing}>
        <span class="refresh-icon {syncing?'spin':''}"><RefreshCw size={13} strokeWidth={2} /></span>
        {syncing ? syncLog.slice(0,22) || 'Syncing…' : 'Refresh'}
      </button>
    </div>
  </div>

  <!-- Tile grid with infinite scroll -->
  <div class="grid scroll-y">
    {#each items as item (item.id ?? item.asset_id + item.date)}
      {@const isNew       = item._isNew === true}
      {@const isFirstSale = Object.values(item.by_stock||{}).reduce((/** @type {number} */ s, /** @type {any} */ d) => s + d.count, 0) === 1}
      {@const inGroups    = Object.entries($photoGroups).filter(([,ids]) => ids.includes(item.asset_id)).map(([n]) => n)}
      <div class="card {isNew ? 'is-new' : ''}"
        in:fly={i < 15 ? { y: 14, duration: 180, delay: i * 16 } : { y: 0, duration: 0 }}
        onclick={() => openPopup(item)}
        oncontextmenu={(e) => openCtx(e, item)}
        role="button" tabindex="0"
        onkeydown={(e) => e.key === 'Enter' && openPopup(item)}>
        {#if isFirstSale}<div class="ribbon-new">NEW</div>{/if}
        <div class="img-box">
          <img src={`${API_BASE}/img/cache/${item.asset_id}`}
            onerror={(e) => { const t=/** @type {HTMLImageElement} */(e.target),url=item.thumb_url||''; if(url&&t.src!==url){t.src=url;}else{t.src=API_BASE+'/img/placeholder';t.onerror=null;} }}
            alt="" class="thumb" />
        </div>
        <div class="meta">
          <div class="price {isNew?'new':''}">${(item.price||0).toFixed(2)}</div>
          <div class="info">{item.stock} · {item.date?.slice(5, 10)}</div>
          {#if inGroups.length > 0}<div class="group-badge">📁 {inGroups[0]}</div>{/if}
        </div>
        <div class="dots">
          {#each Object.keys(item.by_stock||{}) as sk}
            <span class="dot" style="background:{STOCK_COLORS[sk]||'#888'}" title={sk}></span>
          {/each}
        </div>
      </div>
    {/each}

    {#if loading}
      <div class="loader">Loading…</div>
    {/if}

    <div class="sentinel" bind:this={sentinel}></div>
  </div>
</div>

<!-- Popup -->
{#if popup}
  <PhotoPopup item={popup} onClose={() => popup = null} />
{/if}

<!-- Context menu (right-click → group management) -->
{#if ctxMenu}
  <GroupContextMenu ctx={ctxMenu} onClose={() => ctxMenu = null} onOpenDetails={openPopup} />
{/if}

<style>
  .tab-wrap { display:flex; flex-direction:column; height:100%; gap:8px; }

  .filters {
    display:flex; flex-direction:column; gap:6px; flex-shrink:0;
    background: var(--glass); border:1px solid var(--glass-border);
    backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border-radius: var(--radius); padding:10px 12px;
    box-shadow: var(--shadow-sm), var(--glass-shine);
  }
  .filter-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .filter-label { font-size:11px; color: var(--label3); min-width:44px; font-weight:600; text-transform:uppercase; letter-spacing:.06em; }

  .refresh-btn { margin-left: auto; }
  .refresh-btn.busy { opacity: .45; cursor: default; }
  .refresh-row { display:inline-flex; align-items:center; gap:5px; }
  .refresh-icon { display:inline-flex; align-items:center; }
  .refresh-icon.spin { animation:spin 1s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .sync-time { font-size:9px; opacity:.7; font-weight:400; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100px; display:block; }

  .grid {
    flex:1; overflow-y:auto;
    display:flex; flex-wrap:wrap;
    gap:8px; align-content:start;
    padding-right:2px;
  }

  .card {
    width:160px; flex-shrink:0; flex-grow:0;
    display:flex; flex-direction:column;
    background: var(--glass); border-radius: var(--radius); border:1px solid var(--glass-border);
    overflow:hidden; cursor:pointer; position:relative;
    transition: transform .18s, box-shadow .18s, border-color .18s;
    box-shadow: var(--shadow-sm), var(--glass-shine), var(--refract);
  }
  .card:hover { border-color: var(--accent); transform:translateY(-3px) scale(1.015); box-shadow: var(--shadow), var(--glass-shine), var(--refract); }
  .card.is-new { border-color: rgba(10,132,255,0.7); box-shadow: 0 0 0 1px rgba(10,132,255,0.25), var(--shadow-sm), var(--glass-shine), var(--refract); }

  .ribbon-new {
    position:absolute; top:6px; left:0;
    background: var(--accent); color:#fff;
    font-size:8px; font-weight:700; letter-spacing:.06em;
    padding:2px 7px 2px 5px; border-radius:0 var(--radius-xs) var(--radius-xs) 0; z-index:2;
    box-shadow: 0 1px 4px rgba(10,132,255,0.5);
  }

  .img-box { width:160px; height:107px; background: var(--bg3); flex-shrink:0; overflow:hidden; }
  .thumb   { width:100%; height:100%; object-fit:cover; display:block; }

  .meta    { padding:7px 9px 9px; }
  .price   { font-size:15px; font-weight:700; color: var(--label); line-height:1.2; letter-spacing:-0.3px; }
  .price.new { color: var(--accent); }
  .info    { font-size:10px; color: var(--label2); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-top:1px; }
  .group-badge { font-size:9px; color: var(--accent); margin-top:3px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; opacity:.8; }

  .dots  { position:absolute; top:5px; right:5px; display:flex; gap:3px; }
  .dot   { width:6px; height:6px; border-radius:50%; box-shadow:0 1px 3px rgba(0,0,0,.4); }

  .sentinel { width:100%; height:1px; flex-basis:100%; }
  .loader   { width:100%; color: var(--label3); font-size:12px; text-align:center; padding:20px; flex-basis:100%; }
</style>
