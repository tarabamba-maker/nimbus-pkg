<script>
  import { API_BASE } from "$lib/api.js";
  import { scale } from 'svelte/transition';
  import { backOut } from 'svelte/easing';
  import { X, Download, Upload, Check } from 'lucide-svelte';
  import { stockColors, saveStockColor } from '$lib/stockColors.js';

  let { onClose, stocks = [] } = $props();

  let colors = $derived({ ...$stockColors });
  let importStatus = $state('');

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

  /** @param {KeyboardEvent} e */
  function handleKey(e) { if (e.key === 'Escape') onClose?.(); }
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
