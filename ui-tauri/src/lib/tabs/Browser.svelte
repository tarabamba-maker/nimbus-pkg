<script>
  import { API_BASE } from "$lib/api.js";
  import { onMount } from 'svelte';
  import { RefreshCw, StopCircle, Monitor, EyeOff, Search, X } from 'lucide-svelte';
  import { stockColors } from '$lib/stockColors.js';
  import { notifySyncDone, syncLog, syncRunning, syncProgress,
           startSyncStream, stopSyncStream, clearSyncLog } from '$lib/stores/appState.js';

  let { onSyncDone } = $props();

  // Global sync state (persists across tab switches)
  let logs       = $derived($syncLog);
  let running    = $derived($syncRunning);
  let progress   = $derived($syncProgress);
  let headless   = $state(true);

  // Inspector state — universal: one blank inspector, user types any URL
  let inspectorRunning = $state(false);
  let inspectorUrl     = $state('');
  let inspectorLogs    = $state(/** @type {string[]} */ ([]));
  let inspectorEs      = $state(/** @type {EventSource|null} */ (null));

  const STOCKS = ['Adobe Stock', 'Shutterstock', 'Getty Images', 'Depositphotos', 'Envato', 'Freepik', '123RF', 'PIXTA', 'Dreamstime', 'Alamy'];
  const STOCK_COLORS = $derived($stockColors);

  let debugCopyMsg = $state('');
  async function copyDebugLog() {
    try {
      const txt = await fetch(API_BASE + '/api/debug/log?n=400').then(r => r.text());
      await navigator.clipboard.writeText(txt);
      debugCopyMsg = '✓ Copied';
    } catch (e) {
      debugCopyMsg = '✗ ' + (e.message || 'error');
    }
    setTimeout(() => { debugCopyMsg = ''; }, 2000);
  }

  /** @param {string|null} [stockName] */
  async function startStream(stockName = null) {
    stopSyncStream();
    clearSyncLog();
    const body = stockName ? JSON.stringify({ stock: stockName }) : undefined;
    const headers = stockName ? { 'Content-Type': 'application/json' } : undefined;
    const r = await fetch(API_BASE + '/api/sync/start', { method: 'POST', headers, body })
      .then(r => r.json()).catch(() => ({}));
    if (r.ok === false) {
      syncLog.set([r.msg || 'Already running']);
      return;
    }
    startSyncStream();  // global SSE — keeps streaming across tab switches
  }

  async function stopSync() {
    stopSyncStream();
    await fetch(API_BASE + '/api/sync/stop', { method: 'POST' }).catch(() => {});
  }

  /** @param {boolean} val */
  async function toggleHeadless(val) {
    headless = val;
    await fetch(API_BASE + '/api/sync/headless', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ headless: val }),
    }).catch(() => {});
  }

  async function startInspector() {
    if (inspectorEs) { inspectorEs.close(); inspectorEs = null; }
    inspectorLogs = [];
    inspectorRunning = true;
    const r = await fetch(API_BASE + '/api/inspector/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stock: 'Universal', url: inspectorUrl.trim() }),
    }).then(r => r.json()).catch(() => ({ ok: false }));
    if (r.ok === false) { inspectorLogs = [r.msg || 'Failed to start']; inspectorRunning = false; return; }
    const source = new EventSource(API_BASE + '/api/inspector/stream');
    inspectorEs = source;
    source.onmessage = (e) => {
      const d = JSON.parse(e.data);
      if (d.done) { source.close(); inspectorEs = null; inspectorRunning = false; }
      else { inspectorLogs = [...inspectorLogs.slice(-2999), d.msg]; }
    };
    source.onerror = () => { source.close(); inspectorEs = null; inspectorRunning = false; };
  }

  async function stopInspector() {
    if (inspectorEs) { inspectorEs.close(); inspectorEs = null; }
    await fetch(API_BASE + '/api/inspector/stop', { method: 'POST' }).catch(() => {});
    inspectorRunning = false;
  }

  onMount(() => {
    // Don't manage sync EventSource here — it lives in appState (global).
    // Only clean up the inspector stream (which is local to this tab).
    return () => {
      if (inspectorEs) { inspectorEs.close(); inspectorEs = null; }
    };
  });
</script>

<div class="browser">
  <!-- Header row -->
  <div class="header">
    <span class="title">Browser Sync</span>
    <div class="headless-toggle">
      <span class="ht-label">Mode:</span>
      <button class="ht-btn {headless?'active':''}" onclick={() => toggleHeadless(true)}><EyeOff size={12} strokeWidth={1.8} /> Headless</button>
      <button class="ht-btn {!headless?'active':''}" onclick={() => toggleHeadless(false)}><Monitor size={12} strokeWidth={1.8} /> Visible</button>
    </div>
    <div class="btns">
      {#if running}
        <button class="pill danger" onclick={stopSync}><StopCircle size={13} strokeWidth={1.8} /> Stop</button>
      {:else}
        <button class="action-pill" onclick={() => startStream()}><RefreshCw size={13} strokeWidth={1.8} /> Sync All</button>
      {/if}
    </div>
  </div>

  <!-- Per-stock buttons -->
  <div class="stock-row">
    {#each STOCKS as s}
      <button
        class="stock-btn"
        style="border-color:{STOCK_COLORS[s]}"
        disabled={running}
        onclick={() => startStream(s)}>
        <span class="stock-dot" style="background:{STOCK_COLORS[s]}"></span>
        {s}
      </button>
    {/each}
  </div>

  <!-- Combined status + log (single panel, always reads global state) -->
  <div class="log-box card scroll-y">
    <div class="log-header">
      <span class="status-dot {running ? 'running' : 'idle'}"></span>
      <span class="status-text">{running ? 'Syncing…' : 'Ready'}</span>
      {#if progress}<span class="progress-inline dim">{progress}</span>{/if}
    </div>
    {#each logs as line, i (i)}
      <div class="log-line {i === logs.length-1 ? 'last' : ''}">{line}</div>
    {/each}
    {#if logs.length === 0}
      <div class="dim" style="font-size:11px">Log will appear after sync starts…</div>
    {/if}
  </div>

  <!-- ── Debug logs ───────────────────────────── -->
  <div class="inspector-section card">
    <div class="inspector-header">
      <span class="inspector-title">Debug logs</span>
      <span class="inspector-sub dim">копіюй сюди коли репортиш баг</span>
      <div class="inspector-controls">
        <button class="action-pill" onclick={copyDebugLog}>{debugCopyMsg || 'Copy logs'}</button>
      </div>
    </div>
  </div>

  <!-- ── Network Inspector ───────────────────────────── -->
  <div class="inspector-section card">
    <div class="inspector-header">
      <Search size={13} strokeWidth={1.8} />
      <span class="inspector-title">Network Inspector</span>
      <span class="inspector-sub dim">універсальний — введи будь-який URL</span>
      <div class="inspector-controls">
        <input class="inspector-url" type="text" bind:value={inspectorUrl}
               disabled={inspectorRunning}
               placeholder="URL (необов'язково — або введи в адресному рядку браузера)" />
        {#if inspectorRunning}
          <button class="action-pill" onclick={stopInspector}>
            <X size={12} strokeWidth={2} /> Stop
          </button>
        {:else}
          <button class="action-pill" onclick={startInspector}>
            <Search size={12} strokeWidth={2} /> Open Inspector
          </button>
        {/if}
      </div>
    </div>
    <div class="inspector-log scroll-y">
      {#each inspectorLogs as entry, i (i)}
        <pre class="inspector-entry {i === inspectorLogs.length-1 ? 'last' : ''}">{entry}</pre>
      {/each}
      {#if inspectorLogs.length === 0}
        <div class="dim" style="font-size:11px">Відкрий браузер → введи адресу й залогінься → ⭐ позначає схожі на sales/earnings JSON. Cookies + HAR зберігаються в inspector_logs/</div>
      {/if}
    </div>
  </div>
</div>

<style>
  .browser { display:flex; flex-direction:column; height:100%; gap:10px; }

  .header {
    display:flex; align-items:center; gap:10px; flex-shrink:0; flex-wrap:wrap;
    background: var(--glass); backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border: 1px solid var(--glass-border); border-radius: var(--radius);
    padding: 10px 16px;
  }
  .title { font-size:16px; font-weight:700; color: var(--label); }
  .btns { display:flex; gap:8px; margin-left:auto; }

  /* Headless toggle */
  .headless-toggle { display:flex; align-items:center; gap:6px; }
  .ht-label { font-size:11px; color: var(--label3); }
  .ht-btn {
    display:inline-flex; align-items:center; gap:5px;
    background: rgba(10,132,255,0.08); border: 1px solid rgba(10,132,255,0.35);
    border-radius: var(--radius-pill); padding:4px 12px; cursor:pointer; font-size:11px;
    font-weight:600; color: var(--label2); font:inherit;
    box-shadow: 0 0 10px rgba(10,132,255,0.08); transition: background .15s, color .15s;
  }
  .ht-btn:hover { background: rgba(10,132,255,0.14); color: var(--label); }
  .ht-btn.active {
    background: var(--sel-bg-solid); border-color: var(--sel-border); color: var(--sel-fg);
    box-shadow: 0 0 16px rgba(10,132,255,0.18), inset 0 1px 0 rgba(255,255,255,0.20);
  }

  /* Per-stock buttons */
  .stock-row { display:flex; gap:8px; flex-shrink:0; flex-wrap:wrap; }
  .stock-btn {
    display:flex; align-items:center; gap:6px;
    background: rgba(10,132,255,0.08); border: 1px solid rgba(10,132,255,0.35);
    border-radius: var(--radius-pill);
    padding:6px 14px; cursor:pointer; color: var(--label2); font-size:12px; font-weight:600;
    transition: background .15s, color .15s; font:inherit;
    box-shadow: 0 0 10px rgba(10,132,255,0.08);
  }
  .stock-btn:hover:not(:disabled) { background: rgba(10,132,255,0.15); color: var(--label); }
  .stock-btn:disabled { opacity:.4; cursor:not-allowed; }
  .stock-dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }

  .status-card {
    padding:12px 16px; display:flex; flex-direction:column; gap:6px; flex-shrink:0;
    background: var(--glass); backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border: 1px solid var(--glass-border); border-radius: var(--radius);
  }
  .status-row { display:flex; align-items:center; gap:8px; }
  .status-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
  .status-dot.running { background: var(--accent); box-shadow:0 0 6px var(--accent); }
  .status-dot.idle { background: var(--green); }
  .status-text { font-size:13px; color: var(--label); }
  .progress { font-size:11px; color: var(--label3); }

  .log-box {
    flex:1; overflow-y:auto; font-family:monospace; font-size:11px; padding:12px 14px;
    background: var(--glass); backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border: 1px solid var(--glass-border); border-radius: var(--radius);
  }
  .log-header {
    display:flex; align-items:center; gap:8px;
    padding-bottom:8px; margin-bottom:8px;
    border-bottom: 1px solid var(--sep);
    font-family: -apple-system, sans-serif; font-size:12px;
  }
  .progress-inline {
    flex:1; font-size:11px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    font-family: monospace;
  }
  .log-line { color: var(--label3); line-height:1.7; }
  .log-line.last { color: var(--label); }

  :global(.pill.danger) {
    background: rgba(255,69,58,0.22);
    backdrop-filter: blur(16px) saturate(160%); -webkit-backdrop-filter: blur(16px) saturate(160%);
    border-color: rgba(255,69,58,0.55); color: #fff;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.15), inset 0 -1px 0 rgba(0,0,0,0.15), 0 2px 10px rgba(255,69,58,0.08);
  }
  .dim { color: var(--label3); }

  /* ── Network Inspector ──────────────────────────── */
  .inspector-section {
    flex-shrink: 0; display: flex; flex-direction: column; gap: 8px;
    padding: 10px 14px; max-height: 340px;
    background: var(--glass); border: 1px solid var(--glass-border); border-radius: var(--radius);
  }
  .inspector-header {
    display: flex; align-items: center; gap: 8px; flex-shrink: 0; flex-wrap: wrap;
  }
  .inspector-title { font-size: 13px; font-weight: 700; color: var(--label); }
  .inspector-sub   { font-size: 10px; }
  .inspector-controls { display: flex; align-items: center; gap: 6px; margin-left: auto; }
  .inspector-select {
    background: var(--glass2); border: 1px solid rgba(10,132,255,0.35);
    color: var(--label); border-radius: var(--radius-pill);
    padding: 4px 10px; font-size: 12px; font-weight: 600; font-family: inherit;
    cursor: pointer; outline: none;
  }
  .inspector-url {
    background: var(--glass2); border: 1px solid rgba(10,132,255,0.35);
    color: var(--label); border-radius: var(--radius-pill);
    padding: 4px 12px; font-size: 12px; font-family: inherit;
    outline: none; min-width: 320px;
  }
  .inspector-url::placeholder { color: var(--dim); }
  .inspector-log {
    flex: 1; overflow-y: auto; font-family: monospace; font-size: 10px;
    max-height: 260px;
  }
  .inspector-entry {
    color: var(--label3); line-height: 1.6; white-space: pre-wrap; word-break: break-all;
    border-bottom: 1px solid var(--sep); padding: 4px 0; margin: 0;
  }
  .inspector-entry.last { color: var(--label); }
</style>
