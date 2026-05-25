<script>
  import { scale } from 'svelte/transition';
  import { backOut } from 'svelte/easing';
  import { Link, Link2Off, FolderPlus } from 'lucide-svelte';

  /**
   * Shared popup for Downloads + BestSellers.
   * Shows per-stock breakdown + group management (same DB, same component).
   */
  import { photoGroups, toggleGroupMember, createGroup } from '$lib/stores/appState.js';
  import { API_BASE } from '$lib/api.js';

  let {
    item,                // { asset_id, by_stock, thumb_url, ... }
    matchedIds = [],     // asset_ids this photo is linked to
    onClose,
    onUnlink = undefined,
    onMatch = undefined, // optional: enter match mode from parent
    hideGroups = false,
  } = $props();

  const onToggleGroup = toggleGroupMember;
  const onCreateGroup = createGroup;

  let showNewGroup  = $state(false);
  let newGroupName  = $state('');
  let groupSearch   = $state('');

  /** @param {any} item */
  function hiResUrl(item) {
    const url = item.thumb_url || '';
    if (url.includes('depositphotos.com'))
      return ('https:' + url).replace('/i/110/', '/i/450/');
    // Adobe ftcdn.net: size is encoded as NNN_F_ in the path — swap to 500_F_
    if (url.includes('ftcdn.net'))
      return url.replace(/\/\d+_F_/, '/500_F_');
    return `${API_BASE}/img/cache/${item.asset_id}`;
  }

  import { stockColors } from '$lib/stockColors.js';
  const STOCK_COLORS = $derived($stockColors);

  let inGroups = $derived(
    item?.asset_id
      ? Object.entries($photoGroups)
          .filter(([, ids]) => ids.includes(item.asset_id))
          .map(([name]) => name)
      : []
  );

  function submit() {
    const name = newGroupName.trim();
    if (!name) return;
    // createGroup signature: (name, [assetId]) — array-destructures the second arg,
    // so we must pass an array, not a bare string.
    onCreateGroup?.(name, [item.asset_id]);
    showNewGroup = false;
    newGroupName = '';
  }

  /** @param {KeyboardEvent} e */
  function handleKey(e) {
    if (e.key === 'Escape') onClose?.();
  }
</script>

<svelte:window onkeydown={handleKey} />

<div class="overlay" onclick={onClose} onkeydown={handleKey} role="dialog" tabindex="-1">
  <div class="panel" transition:scale={{ duration: 300, start: 0.88, easing: backOut }} onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="presentation">

    <!-- Thumbnail -->
    <div class="img-wrap">
      <img
        src={hiResUrl(item)}
        onerror={(e) => {
          const t = /** @type {HTMLImageElement} */ (e.target);
          const cache = `${API_BASE}/img/cache/${item.asset_id}`;
          if (t.src !== cache) { t.src = cache; }
          else { t.src = API_BASE + '/img/placeholder'; t.onerror = null; }
        }}
        alt=""
        class="preview-img" />
    </div>

    <!-- Per-stock breakdown -->
    <div class="stocks">
      {#each Object.entries(item.by_stock || {}) as [sk, d]}
        <div class="stock-row">
          <span class="stock-icon" style="background:{STOCK_COLORS[sk]||'#555'}">
            <img src="{API_BASE}/img/stock-icon/{sk}" alt={sk}
              onerror={(e) => { const t=/** @type {HTMLImageElement} */(e.target); t.style.display='none'; }} />
          </span>
          <span class="stock-name">{sk}</span>
          <span class="stock-val">${d.total.toFixed(2)}</span>
          <span class="dim">· {d.count} sales</span>
        </div>
      {/each}
      {#if Object.keys(item.by_stock || {}).length === 0}
        <div class="dim" style="padding:8px 16px;font-size:12px">No sales</div>
      {/if}
    </div>

    <!-- Match info + Unlink + Match button -->
    <div class="match-row">
      {#if matchedIds.length > 0}
        <span class="match-label"><Link size={10} strokeWidth={2} /> {matchedIds.slice(0,2).join(', ')}{matchedIds.length > 2 ? ` +${matchedIds.length-2}` : ''}</span>
        {#if onUnlink}
          <button class="unlink-btn" onclick={onUnlink}><Link2Off size={11} strokeWidth={2} /> Unlink</button>
        {/if}
      {:else if onMatch}
        <span class="match-label dim">Not linked</span>
      {/if}
      {#if onMatch}
        <button class="match-btn" onclick={onMatch}><Link size={11} strokeWidth={2} /> Match</button>
      {/if}
    </div>

    {#if !hideGroups}
    <div class="divider"></div>

    <!-- Group search + current groups label -->
    <div class="group-search-row">
      <input class="group-search" placeholder="Search groups…" bind:value={groupSearch} />
      {#if inGroups.length > 0}
        <span class="in-groups-badge">{inGroups.length}</span>
      {/if}
    </div>

    <!-- Group list -->
    <div class="group-list">
      {#each Object.keys($photoGroups).filter(g => !groupSearch || g.toLowerCase().includes(groupSearch.toLowerCase())) as gname}
        <button class="group-btn" onclick={() => onToggleGroup?.(gname, item.asset_id)}>
          <span class="check">{inGroups.includes(gname) ? '✓' : ''}</span>
          <span class="folder-icon">📁</span>
          {gname}
        </button>
      {/each}
    </div>

    <!-- Create new group -->
    {#if showNewGroup}
      <div class="new-group-row" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="presentation">
        <input
          class="new-group-input"
          placeholder="Group name…"
          bind:value={newGroupName}
          onkeydown={(e) => e.key === 'Enter' && submit()} />
        <button class="btn-sm primary" onclick={submit}>OK</button>
        <button class="btn-sm" onclick={() => { showNewGroup = false; newGroupName = ''; }}>✕</button>
      </div>
    {:else}
      <button class="create-btn" onclick={() => showNewGroup = true}><FolderPlus size={12} strokeWidth={1.8} /> Create new group…</button>
    {/if}
    {/if}

  </div>
</div>

<style>
  .overlay {
    position: fixed; inset: 0; z-index: 300;
    backdrop-filter: blur(16px) saturate(160%); -webkit-backdrop-filter: blur(16px) saturate(160%);
    display: flex; align-items: center; justify-content: center;
  }
  .panel {
    background: var(--glass2);
    backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border: 1px solid var(--glass-border); border-radius: var(--radius);
    width: 320px; max-height: 80vh;
    display: flex; flex-direction: column;
    box-shadow: var(--shadow), inset 0 1px 0 rgba(255,255,255,0.07);
    overflow: hidden;
  }

  .img-wrap {
    width: 100%; height: 200px; background: var(--bg); flex-shrink: 0;
    display: flex; align-items: center; justify-content: center; overflow: hidden;
  }
  .preview-img { width: 100%; height: 100%; object-fit: cover; display: block; }

  .stocks { flex-shrink: 0; }
  .stock-row {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 16px;
  }
  .stock-icon {
    width: 24px; height: 24px; border-radius: var(--radius-xs);
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 9px; font-weight: 700; color: #fff; flex-shrink: 0;
    overflow: hidden; padding: 3px;
  }
  .stock-icon img { width: 100%; height: 100%; object-fit: contain; border-radius: 2px; }
  .stock-name { font-size: 12px; color: var(--label); flex: 1; }
  .stock-val  { font-size: 13px; font-weight: 700; color: var(--accent); }

  .divider { height: 1px; background: var(--sep); margin: 3px 0; flex-shrink: 0; }

  .group-search-row {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 12px; flex-shrink: 0;
  }
  .group-search {
    flex: 1; background: var(--bg3); border: 1px solid var(--glass-border);
    color: var(--label); border-radius: var(--radius-pill);
    padding: 5px 12px; font-size: 11px; font-family: inherit; outline: none;
  }
  .group-search:focus { border-color: rgba(10,132,255,0.5); }
  .in-groups-badge {
    background: var(--accent); color: #fff; border-radius: 999px;
    font-size: 10px; font-weight: 700; min-width: 18px; height: 18px;
    display: inline-flex; align-items: center; justify-content: center; padding: 0 5px;
    flex-shrink: 0;
  }

  .group-list { overflow-y: auto; max-height: 180px; flex-shrink: 0; }
  .group-btn {
    display: flex; align-items: center; gap: 6px; width: 100%;
    background: none; border: none; text-align: left;
    padding: 8px 16px; color: var(--label2); font-size: 12px; cursor: pointer; font-family: inherit;
  }
  .group-btn:hover { background: rgba(255,255,255,0.05); color: var(--label); }
  .check { width: 14px; font-size: 11px; color: var(--accent); flex-shrink: 0; }
  .folder-icon { flex-shrink: 0; }

  .create-btn {
    display: block; width: 100%; background: none; border: none;
    text-align: left; padding: 8px 16px; color: var(--accent);
    font-size: 12px; cursor: pointer; flex-shrink: 0; font-family: inherit;
  }
  .create-btn:hover { background: rgba(255,255,255,0.05); }

  .new-group-row {
    display: flex; align-items: center; gap: 6px; padding: 8px 14px; flex-shrink: 0;
  }
  .new-group-input {
    flex: 1; background: var(--bg3); border: 1px solid var(--glass-border);
    color: var(--label); border-radius: var(--radius-sm); padding: 5px 8px; font-size: 12px;
  }
  .btn-sm {
    background: var(--glass); border: 1px solid var(--glass-border);
    color: var(--label2); border-radius: var(--radius-sm); padding: 4px 10px;
    cursor: pointer; font-size: 12px; font-family: inherit;
  }
  .btn-sm.primary { background: var(--accent); border-color: var(--accent); color: #fff; }

  .match-row {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 16px; background: var(--accent-dim); flex-shrink: 0; min-height: 32px;
  }
  .match-label { font-size: 10px; color: var(--green); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display:flex; align-items:center; gap:4px; }
  .unlink-btn {
    background: rgba(255,69,58,0.15); border: 1px solid var(--red); color: var(--red);
    border-radius: var(--radius-sm); padding: 3px 10px; cursor: pointer; font-size: 10px; flex-shrink: 0; font-family: inherit;
    display:inline-flex; align-items:center; gap:4px;
  }
  .unlink-btn:hover { background: rgba(255,69,58,0.25); }
  .match-btn {
    background: var(--accent-dim); border: 1px solid var(--accent); color: var(--accent);
    border-radius: var(--radius-sm); padding: 3px 10px; cursor: pointer; font-size: 10px; flex-shrink: 0;
    margin-left: auto; font-family: inherit; display:inline-flex; align-items:center; gap:4px;
  }
  .match-btn:hover { background: var(--accent); color: #fff; }
  .create-btn { display:inline-flex; align-items:center; gap:5px; }

  .dim { color: var(--label3); font-size: 11px; }
</style>
