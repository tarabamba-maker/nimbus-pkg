<script>
  import { API_BASE } from "$lib/api.js";
  import { scale } from 'svelte/transition';
  import { backOut } from 'svelte/easing';
  import { X, Download, Upload, Trash2, Eraser, RotateCcw } from 'lucide-svelte';
  import { stockColors, saveStockColor } from '$lib/stockColors.js';

  let { onClose, stocks = [] } = $props();

  let colors = $derived({ ...$stockColors });
  let appVersion = $state('');
  import('@tauri-apps/api/app').then(m => m.getVersion()).then(v => appVersion = v).catch(() => {});
  let importStatus = $state('');
  let resetConfirm = $state(false);
  let resetStatus  = $state('');
  let resetting    = $state(false);
  let dedupStatus  = $state('');
  let deduping     = $state(false);
  let dbResetStatus = $state('');
  let dbResetting   = $state(false);
  let dbResetConfirm = $state(false);
  let rebuildStatus = $state('');
  let rebuilding    = $state(false);
  let rebuildConfirm = $state(false);
  let rebuildGroupsStatus = $state('');
  let rebuildingGroups    = $state(false);
  let rebuildGroupsConfirm = $state(false);

  /** @param {string} stock @param {string} color */
  function onChange(stock, color) {
    saveStockColor(stock, color);
  }

  let exportStatus = $state('');

  async function doExport() {
    exportStatus = 'Packing…';
    try {
      // Try native Tauri save dialog first
      let savePath = null;
      try {
        const { save } = await import('@tauri-apps/plugin-dialog');
        savePath = await save({
          defaultPath: 'stock_aggregator_backup.zip',
          filters: [{ name: 'ZIP Archive', extensions: ['zip'] }],
        });
      } catch {}

      const r = await fetch(API_BASE + '/api/export');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const arrayBuf = await r.arrayBuffer();

      if (savePath) {
        // Write directly to chosen path via Tauri fs
        const { writeFile } = await import('@tauri-apps/plugin-fs');
        await writeFile(savePath, new Uint8Array(arrayBuf));
        exportStatus = `✓ Saved to ${savePath.split('/').pop()}`;
      } else {
        // Fallback: browser download
        const blob = new Blob([arrayBuf], { type: 'application/zip' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'stock_aggregator_backup.zip';
        document.body.appendChild(a); a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        exportStatus = '✓ Downloaded';
      }
    } catch (e) {
      exportStatus = `✗ ${String(e)}`;
    }
    setTimeout(() => exportStatus = '', 5000);
  }

  async function doImport() {
    importStatus = '';
    let filePath = null;
    let fileBytes = null;

    // 1) Try Tauri's native open dialog + fs read — most reliable inside the .app
    try {
      const { open } = await import('@tauri-apps/plugin-dialog');
      filePath = await open({
        multiple: false,
        filters: [{ name: 'ZIP Archive', extensions: ['zip'] }],
      });
      if (!filePath) return;  // user cancelled
      const { readFile } = await import('@tauri-apps/plugin-fs');
      fileBytes = await readFile(filePath);  // Uint8Array
    } catch (e) {
      // Not running inside Tauri (dev in browser) — fall back to <input type=file>
      console.debug('[import] tauri dialog unavailable, falling back to browser picker:', e);
    }

    // 2) Browser fallback (dev/non-Tauri)
    if (!fileBytes) {
      const file = await new Promise((resolve) => {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = '.zip';
        input.onchange = () => resolve(input.files?.[0] || null);
        input.click();
      });
      if (!file) return;
      fileBytes = new Uint8Array(await file.arrayBuffer());
      filePath = file.name;
    }

    importStatus = 'Importing…';
    try {
      // Upload as raw bytes — simpler than FormData and works identically in Tauri webview
      const resp = await fetch(API_BASE + '/api/import-raw', {
        method: 'POST',
        headers: { 'Content-Type': 'application/zip' },
        body: fileBytes,
      });
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}`;
        try { const j = await resp.json(); if (j.msg) msg = j.msg; } catch {}
        importStatus = `✗ ${msg}`;
      } else {
        const r = await resp.json();
        importStatus = r.ok ? `✓ Imported ${r.files} files — restart app` : `✗ ${r.msg || 'unknown error'}`;
      }
    } catch (e) {
      importStatus = `✗ ${String(e)}`;
      console.error('[import] upload failed:', e);
    }
  }

  async function doDedup() {
    deduping = true; dedupStatus = '';
    try {
      const r = await fetch(API_BASE + '/api/deduplicate', { method: 'POST' }).then(r => r.json());
      if (r.status === 'ok') {
        const parts = [];
        if (r.sales_removed)       parts.push(`${r.sales_removed} sale rows removed`);
        if (r.groups_cross_removed) parts.push(`${r.groups_cross_removed} cross-group dups`);
        dedupStatus = parts.length ? `✓ ${parts.join(', ')}` : '✓ Nothing to clean';
      } else {
        dedupStatus = `✗ ${r.msg || 'error'}`;
      }
    } catch (e) { dedupStatus = `✗ ${String(e)}`; }
    deduping = false;
    setTimeout(() => dedupStatus = '', 8000);
  }

  async function doResetDb() {
    dbResetting = true; dbResetStatus = '';
    try {
      const r = await fetch(API_BASE + '/api/reset-db', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: 'RESET' }),
      }).then(r => r.json());
      if (r.status === 'ok') {
        // Clear ALL frontend caches so UI doesn't show stale numbers
        try {
          for (const k of Object.keys(localStorage)) {
            if (k.startsWith('sa:') || k.includes('cache') || k.includes('downloads') || k.includes('bestsellers') || k.includes('groups')) {
              localStorage.removeItem(k);
            }
          }
          sessionStorage.clear();
        } catch {}
        dbResetStatus = '✓ База очищена. Перезавантажую…';
        setTimeout(() => location.reload(), 800);
      } else {
        dbResetStatus = `✗ ${r.msg || 'error'}`;
      }
    } catch (e) {
      dbResetStatus = `✗ ${String(e)}`;
    }
    dbResetting = false;
    dbResetConfirm = false;
  }

  async function doRebuildFromScratch() {
    rebuilding = true; rebuildStatus = 'Очищаю БД…';
    try {
      // 1. Wipe everything except logins
      const r1 = await fetch(API_BASE + '/api/reset-db', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: 'RESET' }),
      }).then(r => r.json());
      if (r1.status !== 'ok') {
        rebuildStatus = `✗ reset: ${r1.msg || 'error'}`;
        rebuilding = false; rebuildConfirm = false;
        return;
      }
      // Clear UI cache
      try {
        for (const k of Object.keys(localStorage)) {
          if (k.startsWith('sa:') || k.includes('cache') || k.includes('downloads') || k.includes('bestsellers') || k.includes('groups')) {
            localStorage.removeItem(k);
          }
        }
        sessionStorage.clear();
      } catch {}
      rebuildStatus = 'Стартую повний синк (5 стоків)…';
      // 2. Trigger full sync (auto-runs rebuild-matches at the end)
      const r2 = await fetch(API_BASE + '/api/sync/start', { method: 'POST' }).then(r => r.json()).catch(() => ({ok: false}));
      if (r2.ok === false && r2.msg) {
        rebuildStatus = `✓ Очищено. Синк: ${r2.msg}`;
      } else {
        rebuildStatus = '✓ Очищено + синк стартував. Перезавантажую UI…';
      }
      setTimeout(() => location.reload(), 1500);
    } catch (e) {
      rebuildStatus = `✗ ${String(e)}`;
    }
    rebuilding = false;
    rebuildConfirm = false;
  }

  async function doRebuildGroups() {
    rebuildingGroups = true; rebuildGroupsStatus = 'Очищаю групи + MS+ → старт re-sync…';
    try {
      const r = await fetch(API_BASE + '/api/rebuild-groups', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: 'REBUILD' }),
      }).then(r => r.json());
      if (r.status === 'ok') {
        rebuildGroupsStatus = '✓ Запущено у фоні. Слідкуй у Browser tab за прогресом';
      } else {
        rebuildGroupsStatus = `✗ ${r.msg || 'error'}`;
      }
    } catch (e) {
      rebuildGroupsStatus = `✗ ${String(e)}`;
    }
    rebuildingGroups = false;
    rebuildGroupsConfirm = false;
    setTimeout(() => rebuildGroupsStatus = '', 8000);
  }

  async function doReset() {
    resetting = true; resetStatus = '';
    try {
      const r = await fetch(API_BASE + '/api/reset', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: 'RESET' }),
      }).then(r => r.json());
      if (r.status === 'ok') {
        resetStatus = `✓ Reset done — restart app`;
      } else {
        resetStatus = `✗ ${r.msg || 'error'}`;
      }
    } catch (e) {
      resetStatus = `✗ ${String(e)}`;
    }
    resetting = false;
    resetConfirm = false;
    setTimeout(() => resetStatus = '', 8000);
  }

  /** @param {KeyboardEvent} e */
  function handleKey(e) { if (e.key === 'Escape') { if (resetConfirm) { resetConfirm = false; return; } onClose?.(); } }
</script>

<svelte:window onkeydown={handleKey} />

<div class="overlay" onclick={onClose} role="dialog" tabindex="-1">
  <div
    class="panel"
    transition:scale={{ duration: 260, start: 0.9, easing: backOut }}
    onclick={(e) => e.stopPropagation()}
    role="presentation">

    <div class="header">
      <span class="title">Settings</span>
      {#if appVersion}<span class="version">v{appVersion}</span>{/if}
      <button class="close-btn" onclick={onClose}><X size={14} strokeWidth={2} /></button>
    </div>

    <!-- Export / Import -->
    <div class="section-label">Database</div>
    <div class="db-row">
      <button class="db-btn" onclick={doExport}><Download size={13} strokeWidth={2} /> Export backup</button>
      <button class="db-btn" onclick={doImport}><Upload size={13} strokeWidth={2} /> Import backup</button>
    </div>
    {#if exportStatus || importStatus}
      <div class="import-status" class:ok={(exportStatus||importStatus).startsWith('✓')}>
        {exportStatus || importStatus}
      </div>
    {/if}
    <div class="db-row">
      <button class="db-btn" onclick={doDedup} disabled={deduping}>
        <Eraser size={13} strokeWidth={2} /> {deduping ? 'Cleaning…' : 'Deduplicate'}
      </button>
    </div>
    {#if dedupStatus}
      <div class="import-status" class:ok={dedupStatus.startsWith('✓')}>{dedupStatus}</div>
    {/if}
    {#if !dbResetConfirm}
      <div class="db-row">
        <button class="db-btn danger" onclick={() => dbResetConfirm = true} disabled={dbResetting}>
          <Trash2 size={13} strokeWidth={2} /> Reset DB (keep logins)
        </button>
      </div>
    {:else}
      <div class="reset-confirm">
        <span class="reset-warn">Очистити всі продажі? Логіни/групи/MS+ залишаться.</span>
        <div class="confirm-btns-row">
          <button class="btn-cancel-sm" onclick={() => dbResetConfirm = false}>Cancel</button>
          <button class="btn-reset" onclick={doResetDb} disabled={dbResetting}>
            {dbResetting ? 'Resetting…' : 'Yes, wipe DB'}
          </button>
        </div>
      </div>
    {/if}
    {#if dbResetStatus}
      <div class="import-status" class:ok={dbResetStatus.startsWith('✓')}>{dbResetStatus}</div>
    {/if}
    {#if !rebuildConfirm}
      <div class="db-row">
        <button class="db-btn" onclick={() => rebuildConfirm = true} disabled={rebuilding}>
          <RotateCcw size={13} strokeWidth={2} /> Build database from scratch
        </button>
      </div>
    {:else}
      <div class="reset-confirm">
        <span class="reset-warn">Скинути ВСЕ і стартувати повний синк? Збереже тільки логіни.</span>
        <div class="confirm-btns-row">
          <button class="btn-cancel-sm" onclick={() => rebuildConfirm = false}>Cancel</button>
          <button class="btn-reset" onclick={doRebuildFromScratch} disabled={rebuilding}>
            {rebuilding ? rebuildStatus : 'Yes, rebuild all'}
          </button>
        </div>
      </div>
    {/if}
    {#if rebuildStatus && !rebuilding}
      <div class="import-status" class:ok={rebuildStatus.startsWith('✓')}>{rebuildStatus}</div>
    {/if}
    {#if !rebuildGroupsConfirm}
      <div class="db-row">
        <button class="db-btn" onclick={() => rebuildGroupsConfirm = true} disabled={rebuildingGroups}>
          <RotateCcw size={13} strokeWidth={2} /> Rebuild groups (re-sync MS+)
        </button>
      </div>
    {:else}
      <div class="reset-confirm">
        <span class="reset-warn">Перезібрати групи з MS+ заново? Продажі лишаються, ручні зміни груп — обнуляться.</span>
        <div class="confirm-btns-row">
          <button class="btn-cancel-sm" onclick={() => rebuildGroupsConfirm = false}>Cancel</button>
          <button class="btn-reset" onclick={doRebuildGroups} disabled={rebuildingGroups}>
            {rebuildingGroups ? 'Starting…' : 'Yes, rebuild groups'}
          </button>
        </div>
      </div>
    {/if}
    {#if rebuildGroupsStatus}
      <div class="import-status" class:ok={rebuildGroupsStatus.startsWith('✓')}>{rebuildGroupsStatus}</div>
    {/if}

    <!-- Reset -->
    <div class="section-label">Reset</div>
    <div class="reset-desc">Full wipe: sales data, groups, browser sessions, image cache. Only stock colors are kept. Use to switch accounts.</div>
    {#if !resetConfirm}
      <div class="db-row">
        <button class="db-btn danger" onclick={() => resetConfirm = true} disabled={resetting}>
          <Trash2 size={13} strokeWidth={2} /> Reset account data
        </button>
      </div>
    {:else}
      <div class="reset-confirm">
        <span class="reset-warn">This cannot be undone. Continue?</span>
        <div class="confirm-btns-row">
          <button class="btn-cancel-sm" onclick={() => resetConfirm = false}>Cancel</button>
          <button class="btn-reset" onclick={doReset} disabled={resetting}>
            {resetting ? 'Resetting…' : 'Yes, reset'}
          </button>
        </div>
      </div>
    {/if}
    {#if resetStatus}
      <div class="import-status" class:ok={resetStatus.startsWith('✓')}>{resetStatus}</div>
    {/if}

    <div class="section-label">Stock Colors</div>
    <div class="list">
      {#each stocks as stock}
        {@const color = colors[stock] || '#888888'}
        <div class="row">
          <span class="dot" style="background:{color}"></span>
          <span class="name">{stock}</span>
          <input
            type="color"
            class="picker"
            value={color}
            oninput={(e) => onChange(stock, /** @type {HTMLInputElement} */(e.target).value)}
          />
        </div>
      {/each}
    </div>
  </div>
</div>

<style>
  .overlay {
    position: fixed; inset: 0; z-index: 400;
    backdrop-filter: blur(12px) saturate(140%); -webkit-backdrop-filter: blur(12px) saturate(140%);
    display: flex; align-items: center; justify-content: center;
  }
  .panel {
    background: var(--glass2); border: 1px solid var(--glass-border);
    border-radius: var(--radius); width: 300px;
    box-shadow: var(--shadow); overflow: hidden;
    display: flex; flex-direction: column;
  }
  .header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 16px; border-bottom: 1px solid var(--sep); flex-shrink: 0;
  }
  .title { font-size: 14px; font-weight: 700; color: var(--label); }
  .version { font-size: 11px; color: var(--label3); margin-left: auto; margin-right: 8px; font-weight: 500; }
  .close-btn {
    background: none; border: none; color: var(--label3); cursor: pointer;
    padding: 4px; display: flex; align-items: center; border-radius: 4px;
  }
  .close-btn:hover { color: var(--label); background: rgba(255,255,255,0.06); }

  .section-label {
    font-size: 10px; font-weight: 700; color: var(--label3); text-transform: uppercase;
    letter-spacing: .06em; padding: 10px 16px 4px;
  }
  .db-row { display: flex; gap: 8px; padding: 0 16px 8px; }
  .db-btn {
    flex: 1; display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    background: var(--glass); border: 1px solid var(--glass-border);
    color: var(--label2); border-radius: var(--radius-sm);
    padding: 7px 10px; cursor: pointer; font-size: 12px; font-family: inherit;
  }
  .db-btn:hover { background: rgba(10,132,255,0.12); color: var(--label); border-color: rgba(10,132,255,0.4); }
  .import-status {
    font-size: 11px; padding: 0 16px 8px; color: var(--label3);
  }
  .import-status.ok { color: var(--green); }
  .db-btn.danger { color: var(--red, #ff3b30); }
  .db-btn.danger:hover { background: rgba(255,59,48,0.12); border-color: rgba(255,59,48,0.4); color: var(--red, #ff3b30); }
  .reset-desc { font-size: 11px; color: var(--label3); padding: 0 16px 8px; line-height: 1.45; }
  .reset-confirm { padding: 0 16px 8px; display: flex; flex-direction: column; gap: 8px; }
  .reset-warn { font-size: 12px; color: var(--red, #ff3b30); font-weight: 600; }
  .confirm-btns-row { display: flex; gap: 8px; }
  .btn-cancel-sm {
    flex: 1; background: var(--glass); border: 1px solid var(--glass-border);
    color: var(--label2); border-radius: var(--radius-sm); padding: 6px 10px;
    cursor: pointer; font-size: 12px; font-family: inherit;
  }
  .btn-cancel-sm:hover { background: rgba(255,255,255,0.08); }
  .btn-reset {
    flex: 1; background: rgba(255,59,48,0.15); border: 1px solid rgba(255,59,48,0.4);
    color: var(--red, #ff3b30); border-radius: var(--radius-sm); padding: 6px 10px;
    cursor: pointer; font-size: 12px; font-weight: 600; font-family: inherit;
  }
  .btn-reset:hover { background: rgba(255,59,48,0.28); }
  .btn-reset:disabled { opacity: .5; cursor: default; }
  .list { padding: 8px 0; overflow-y: auto; max-height: 40vh; }
  .row {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 16px; cursor: default;
  }
  .row:hover { background: rgba(255,255,255,0.04); }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .name { flex: 1; font-size: 13px; color: var(--label2); }
  .picker {
    width: 32px; height: 24px; border: 1px solid var(--glass-border);
    border-radius: 4px; background: none; cursor: pointer; padding: 1px;
  }
</style>
