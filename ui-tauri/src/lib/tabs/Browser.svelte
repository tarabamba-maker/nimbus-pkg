<script>
  import { API_BASE } from "$lib/api.js";
  import { onMount } from 'svelte';
  import { RefreshCw, StopCircle, Monitor, EyeOff, Search, X } from 'lucide-svelte';
  import { stockColors } from '$lib/stockColors.js';
  import { notifySyncDone } from '$lib/stores/appState.js';

  let { onSyncDone } = $props();

  let syncStatus = $state({ running: false, progress: '' });
  let logs       = $state(/** @type {string[]} */ ([]));
  let es         = $state(/** @type {EventSource|null} */ (null));
  let headless   = $state(true);

  // Inspector state
  let inspectorRunning = $state(false);
  let inspectorStock   = $state('Depositphotos');
  let inspectorLogs    = $state(/** @type {string[]} */ ([]));
  let inspectorEs      = $state(/** @type {EventSource|null} */ (null));

  const INSPECTOR_STOCKS = ['Depositphotos', 'Pond5', 'Envato'];

  const STOCKS = ['Adobe Stock', 'Shutterstock', 'Getty Images', 'Depositphotos'];
  const STOCK_COLORS = $derived($stockColors);

  async function checkStatus() {
    try { syncStatus = await fetch(API_BASE + '/api/sync/status').then(r => r.json()); } catch {}
  }

  /** @param {string|null} [stockName] */
  async function startStream(stockName = null) {
    if (es) { es.close(); es = null; }
    logs = [];
    const body = stockName ? JSON.stringify({ stock: stockName }) : undefined;
    const headers = stockName ? { 'Content-Type': 'application/json' } : undefined;
    const r = await fetch(API_BASE + '/api/sync/start', { method: 'POST', headers, body })
      .then(r => r.json()).catch(() => ({}));
    if (r.ok === false) { logs = [r.msg || 'Already running']; return; }
    const source = new EventSource(API_BASE + '/api/sync/stream');
    es = source;
    source.onmessage = (e) => {
      const d = JSON.parse(e.data);
      if (d.done) {
        source.close(); es = null;
        checkStatus(); onSyncDone?.();
        notifySyncDone();  // broadcast to all tabs
      } else {
        logs = [...logs.slice(-199), d.msg];
      }
    };
    source.onerror = () => { source.close(); es = null; checkStatus(); };
  }

  async function stopSync() {
    if (es) { es.close(); es = null; }
    await fetch(API_BASE + '/api/sync/stop', { method: 'POST' }).catch(() => {});
    checkStatus();
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
      body: JSON.stringify({ stock: inspectorStock }),
    }).then(r => r.json()).catch(() => ({ ok: false }));
    if (r.ok === false) { inspectorLogs = [r.msg || 'Failed to start']; inspectorRunning = false; return; }
    const source = new EventSource(API_BASE + '/api/inspector/stream');
    inspectorEs = source;
    source.onmessage = (e) => {
      const d = JSON.parse(e.data);
      if (d.done) { source.close(); inspectorEs = null; inspectorRunning = false; }
      else { inspectorLogs = [...inspectorLogs.slice(-299), d.msg]; }
    };
    source.onerror = () => { source.close(); inspectorEs = null; inspectorRunning = false; };
  }

  async function stopInspector() {
    if (inspectorEs) { inspectorEs.close(); inspectorEs = null; }
    await fetch(API_BASE + '/api/inspector/stop', { method: 'POST' }).catch(() => {});
    inspectorRunning = false;
  }

  onMount(() => {
    checkStatus();
    return () => {
      if (es) { es.close(); es = null; }
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
      {#if syncStatus.running || es}
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
        disabled={!!(syncStatus.running || es)}
        onclick={() => startStream(s)}>
        <span class="stock-dot" style="background:{STOCK_COLORS[s]}"></span>
        {s}
      </button>
    {/each}
  </div>

  <!-- Status card -->
  <div class="status-card card">
    <div class="status-row">
      <span class="status-dot {syncStatus.running || es ? 'running' : 'idle'}"></span>
      <span class="status-text">{syncStatus.running || es ? 'Syncing…' : 'Ready'}</span>
    </div>
    {#if syncStatus.progress}
      <div class="progress dim">{syncStatus.progress}</div>
    {/if}
  </div>

  <!-- Log -->
  <div class="log-box card scroll-y">
    {#each logs as line, i (i)}
      <div class="log-line {i === logs.length-1 ? 'last' : ''}">{line}</div>
    {/each}
    {#if logs.length === 0}
      <div class="dim" style="font-size:11px">Log will appear after sync starts…</div>
    {/if}
  </div>

  <!-- ── Network Inspector ───────────────────────────── -->
  <div class="inspector-section card">
    <div class="inspector-header">
      <Search size={13} strokeWidth={1.8} />
      <span class="inspector-title">Network Inspector</span>
      <span class="inspector-sub dim">для підключення нових стоків</span>
      <div class="inspector-controls">
        <select class="inspector-select" bind:value={inspectorStock} disabled={inspectorRunning}>
          {#each INSPECTOR_STOCKS as s}
            <option value={s}>{s}</option>
          {/each}
        </select>
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
        <div class="dim" style="font-size:11px">Відкрий браузер → перейди на сторінку з продажами → всі API-запити з'являться тут</div>
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
