<script>
  import { FolderPlus, SquareArrowOutUpRight } from 'lucide-svelte';
  import { photoGroups, toggleGroupMember, createGroup } from '$lib/stores/appState.js';

  /**
   * @typedef {Object} Props
   * @property {{item:any, x:number, y:number}} ctx  - item + click position
   * @property {()=>void} onClose                      - close the menu
   * @property {(item:any)=>void} [onOpenDetails]      - "Open details" handler
   */

  /** @type {Props} */
  let { ctx, onClose, onOpenDetails } = $props();
  let search = $state('');

  let aid = $derived(ctx.item.asset_id);
  let filtered = $derived(
    Object.keys($photoGroups).filter(g => !search || g.toLowerCase().includes(search.toLowerCase()))
  );

  /** @param {string} gname */
  function toggle(gname) {
    toggleGroupMember(gname, aid);
    onClose();
  }
  function create() {
    const name = search.trim() || prompt('Group name:')?.trim();
    if (name) createGroup(name, aid);
    onClose();
  }
  function openDetails() {
    onOpenDetails?.(ctx.item);
    onClose();
  }
</script>

<div class="overlay" onclick={onClose}
  onkeydown={(e) => e.key === 'Escape' && onClose()}
  role="presentation">
  <div class="menu" style="left:{ctx.x}px;top:{ctx.y}px"
    onclick={(e) => e.stopPropagation()}
    onkeydown={(e) => e.stopPropagation()}
    role="menu" tabindex="-1">

    <div class="search-wrap">
      <input class="search" placeholder="Search group…" bind:value={search}
        onclick={(e) => e.stopPropagation()} />
    </div>

    <div class="list">
      {#each filtered as gname}
        {@const inGrp = $photoGroups[gname]?.includes(aid)}
        <button class="btn {inGrp?'in':''}" role="menuitem" onclick={() => toggle(gname)}>
          <span class="check">{inGrp ? '✓' : ''}</span>
          {gname}
        </button>
      {/each}
      {#if filtered.length === 0}
        <div class="empty">{search ? 'Not found' : 'No groups yet'}</div>
      {/if}
    </div>

    <div class="sep"></div>

    <button class="btn new" role="menuitem" onclick={create}>
      <FolderPlus size={12} strokeWidth={1.8} />
      {#if search.trim()}Create "{search.trim()}"
      {:else}New group…{/if}
    </button>

    {#if onOpenDetails}
      <div class="sep"></div>
      <button class="btn open" role="menuitem" onclick={openDetails}>
        <SquareArrowOutUpRight size={12} strokeWidth={1.8} /> Open details
      </button>
    {/if}
  </div>
</div>

<style>
  .overlay { position:fixed; inset:0; z-index:400; }
  .menu {
    position:fixed; z-index:401;
    background: var(--glass2);
    backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border:1px solid var(--glass-border); border-radius: var(--radius);
    width:228px; padding:4px 0;
    box-shadow: var(--shadow), inset 0 1px 0 rgba(255,255,255,0.07);
    display:flex; flex-direction:column; max-height:360px;
  }
  .search-wrap { padding:7px 10px 4px; flex-shrink:0; }
  .search {
    width:100%; background: var(--bg3); border:1px solid var(--glass-border);
    color: var(--label); border-radius: var(--radius-sm); padding:5px 9px; font-size:11px;
  }
  .list { overflow-y:auto; flex:1; max-height:220px; }
  .btn {
    display:flex; align-items:center; gap:7px; width:100%;
    background:none; border:none; text-align:left;
    padding:7px 14px; color: var(--label2); font-size:12px; cursor:pointer;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-family:inherit;
  }
  .btn:hover { background: rgba(255,255,255,0.06); color: var(--label); }
  .btn.in { color: var(--accent); }
  .btn.new { color: var(--green); font-size:11px; flex-shrink:0; }
  .btn.open { color: var(--label3); font-size:11px; flex-shrink:0; }
  .check { width:12px; font-size:11px; color: var(--accent); flex-shrink:0; }
  .sep { height:1px; background: var(--sep); margin:3px 0; flex-shrink:0; }
  .empty { font-size:11px; color: var(--label3); padding:6px 14px; }
</style>
