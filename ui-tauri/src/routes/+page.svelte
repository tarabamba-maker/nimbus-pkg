<script>
  import { API_BASE } from "$lib/api.js";
  import { onMount } from 'svelte';
  import { fly } from 'svelte/transition';
  import { cubicOut, cubicIn } from 'svelte/easing';
  import { ArrowDownToLine, Star, RefreshCw, Sun, Moon, Settings } from 'lucide-svelte';
  import Downloads from '$lib/tabs/Downloads.svelte';
  import BestSellers from '$lib/tabs/BestSellers.svelte';
  import Groups from '$lib/tabs/Groups.svelte';
  import Browser from '$lib/tabs/Browser.svelte';
  import StockColorSettings from '$lib/StockColorSettings.svelte';
  import UpdateBanner from '$lib/components/UpdateBanner.svelte';
  import { stockColors, loadStockColors, STOCK_ABBR } from '$lib/stockColors.js';
  import { untrack } from 'svelte';
  import { loadPhotoGroups, loadStockList, loadMatches, currentPeriod, appReady, resumeSyncIfRunning, startSyncPoller, syncTick } from '$lib/stores/appState.js';

  let activeTab      = $state(0);
  let tabDir         = $state(1);
  let tabBarInner    = $state(/** @type {HTMLElement|null} */ (null));
  let tabSlider      = $state({ left: 0, width: 0 });
  let _sliderReady   = $state(false);  // true after first measurement — enables CSS transition

  /** @param {number} i */
  function switchTab(i) {
    if (i === activeTab) return;
    tabDir = i > activeTab ? 1 : -1;
    if (i !== 2) activeGroup = null;
    activeTab = i;
  }

  // Read both activeTab and tabBarInner synchronously so Svelte tracks both as dependencies.
  // Using rAF instead of tick() ensures the browser has completed layout before measuring.
  $effect(() => {
    activeTab;
    const bar = tabBarInner;
    if (!bar) return;
    (async () => {
      await new Promise(r => requestAnimationFrame(r));
      const btn = /** @type {HTMLElement|null} */ (bar.querySelector('.tab-btn.active'));
      if (!btn) return;
      tabSlider = { left: btn.offsetLeft, width: btn.offsetWidth };
      if (!_sliderReady) {
        await new Promise(r => requestAnimationFrame(r));
        _sliderReady = true;
      }
    })();
  });
  let downloadsStock  = $state('All');
  // Theme: persisted in localStorage so user's choice survives app restart.
  // Default to dark on first launch; load saved preference on mount.
  let darkMode        = $state(true);
  let activeGroup     = $state(/** @type {any} */ (null)); // group open in Groups tab

  let stats = $state(/** @type {Record<string,any>} */ ({
    today: { total:0, count:0, delta:0 },
    week:  { total:0, count:0, delta:0 },
    month: { total:0, count:0, delta:0 },
    year:  { total:0, count:0, delta:0 },
    all:   { total:0, count:0, delta:0 },
    by_stock: [],
  }));

  let STOCK_COLORS = /** @type {Record<string,string>} */ ($derived($stockColors));
  let showColorSettings = $state(false);

  const PERIOD_MAP = /** @type {Record<string,string>} */ ({ today: 'Today', week: 'Week', month: 'Month', year: 'Year', all: 'All-time' });

  const TABS = [
    { label: 'Downloads',    Icon: ArrowDownToLine },
    { label: 'Best Sellers', Icon: Star },
    { label: 'Groups',       emoji: '📁' },
    { label: 'Browser',      Icon: RefreshCw },
  ];

  // Delta matrix: per-period per-filter NEW earnings since BASELINE.
  // Baseline (_baselineStats) is what we compare against — it only advances
  // when we actually see growth. This prevents the bug where two rapid
  // loadStats() calls would compute delta vs each other (0) and wipe the green.
  // Shape: { today: { All: 12.5, 'Adobe Stock': 7.0, ... }, week: {...}, all: {...} }
  let deltasMatrix     = $state(/** @type {Record<string, Record<string, number>>} */ ({}));
  let _statsBaseline   = false;
  let _baselineStats   = /** @type {any} */ (null);  // snapshot we diff against

  async function loadStats() {
    try {
      const fresh = await fetch(API_BASE + '/api/stats').then(r => r.json());

      if (!_statsBaseline) {
        _baselineStats = fresh;
        _statsBaseline = true;
        stats = fresh;
        console.log('[+page] loadStats baseline set');
        return;
      }

      // Compute deltas vs persistent baseline (not vs last-shown stats).
      const newD = /** @type {Record<string, Record<string, number>>} */ ({});
      for (const k of ['today', 'week', 'month', 'year']) {
        const perFilter = /** @type {Record<string, number>} */ ({});
        const dAll = (fresh[k]?.total ?? 0) - (_baselineStats[k]?.total ?? 0);
        if (dAll > 0.005) perFilter.All = dAll;
        const freshBs = fresh[k]?.by_stock ?? {};
        const oldBs   = _baselineStats[k]?.by_stock ?? {};
        for (const s of Object.keys(freshBs)) {
          const d = (freshBs[s]?.total ?? 0) - (oldBs[s]?.total ?? 0);
          if (d > 0.005) perFilter[s] = d;
        }
        if (Object.keys(perFilter).length) newD[k] = perFilter;
      }
      const perFilterAll = /** @type {Record<string, number>} */ ({});
      const dAllTime = (fresh.all?.total ?? 0) - (_baselineStats.all?.total ?? 0);
      if (dAllTime > 0.005) perFilterAll.All = dAllTime;
      const oldByStock = Object.fromEntries((_baselineStats.by_stock || []).map((/** @type {any} */ b) => [b.stock, b]));
      for (const bs of (fresh.by_stock || [])) {
        const d = (bs.total ?? 0) - (oldByStock[bs.stock]?.total ?? 0);
        if (d > 0.005) perFilterAll[bs.stock] = d;
      }
      if (Object.keys(perFilterAll).length) newD.all = perFilterAll;

      // Only update deltasMatrix when we actually computed new growth.
      // Repeated loadStats() with unchanged data → preserve previously-shown
      // green numbers instead of wiping them.
      if (Object.keys(newD).length > 0) {
        deltasMatrix   = newD;
        _baselineStats = fresh;   // advance baseline so next delta is incremental
      }
      stats = fresh;
      console.log('[+page] loadStats OK: today=$' + (fresh.today?.total ?? 0));
    } catch (e) {
      console.error('[+page] loadStats FAILED:', e);
    }
  }

  /** Returns {total, count} for a given period under the current stock filter. */
  function statFor(/** @type {string} */ periodKey) {
    const f = downloadsStock || 'All';
    if (f === 'All') return stats[periodKey] ?? { total:0, count:0 };
    if (periodKey === 'all') {
      const e = (stats.by_stock || []).find((/** @type {any} */ b) => b.stock === f);
      return e ?? { total:0, count:0 };
    }
    return stats[periodKey]?.by_stock?.[f] ?? { total:0, count:0 };
  }

  /** Returns the delta for a given period under the current stock filter. */
  function deltaFor(/** @type {string} */ periodKey) {
    const f = downloadsStock || 'All';
    return deltasMatrix[periodKey]?.[f] ?? 0;
  }

  /** @param {string} stock */
  function onDownloadsStockChange(stock) {
    downloadsStock = stock;
    // No refetch — display is computed from the existing snapshot.
  }

  /** @param {string} key */
  function clickStatCard(key) {
    if (activeGroup) return; // group view — cards are decorative
    currentPeriod.set(PERIOD_MAP[key] || 'All-time');
  }

  // Load persisted theme on first render (browser-only).
  $effect(() => {
    try {
      const saved = localStorage.getItem('app_theme');
      if (saved === 'light') darkMode = false;
      else if (saved === 'dark') darkMode = true;
    } catch {}
  });

  // Apply theme + persist on every change.
  $effect(() => {
    if (darkMode) {
      document.body.classList.remove('light');
    } else {
      document.body.classList.add('light');
    }
    try { localStorage.setItem('app_theme', darkMode ? 'dark' : 'light'); } catch {}
  });

  // Refresh top stats whenever ANY tab triggers a sync completion.
  // Without this, deltas only refresh when Downloads tab calls onRefresh —
  // syncs from Browser tab or background pollers leave stat boxes stale.
  let _statsTick = 0;
  $effect(() => {
    const t = $syncTick;
    if (t > 0 && t !== _statsTick) { _statsTick = t; untrack(() => loadStats()); }
  });

  /** Wait until Flask responds (lib.rs spawns python concurrently with webview
   *  init, so initial fetches may race the backend). Retry every 300ms up to 15s. */
  async function waitForBackend() {
    for (let i = 0; i < 50; i++) {
      try {
        const base = (await import('$lib/api.js')).API_BASE;
        const r = await fetch(base + '/api/stock-list');
        if (r.ok) { console.log('[+page] backend ready after', i * 300, 'ms'); return; }
      } catch {}
      await new Promise(r => setTimeout(r, 300));
    }
    console.warn('[+page] backend not reachable after 15s');
  }

  onMount(async () => {
    await waitForBackend();
    appReady.set(true);
    await Promise.all([
      loadStats(),
      loadPhotoGroups(),
      loadStockColors(),
      loadStockList(),
      loadMatches(),
    ]);
    // Resume sync log stream if a sync was already running (survives UI reload)
    resumeSyncIfRunning();
    // Auto-detect syncs started elsewhere (e.g. via curl or rebuild-groups)
    startSyncPoller();
  });
</script>

<div class="app">
  <UpdateBanner />
  <!-- Top bar -->
  <div class="topbar">
    <span class="app-title">Stock Aggregator</span>
    <div class="topbar-right">
      <button class="icon-btn" onclick={() => darkMode = !darkMode} title="Toggle theme">
        {#if darkMode}<Sun size={15} strokeWidth={1.8} />{:else}<Moon size={15} strokeWidth={1.8} />{/if}
      </button>
<button class="icon-btn" onclick={() => showColorSettings = true} title="Stock colors"><Settings size={14} strokeWidth={1.8} /></button>
    </div>
  </div>

  <!-- Stats cards -->
  <div class="stats-row">
    {#if activeGroup}
      <!-- Group mode: show stock breakdown for the selected group -->
      {@const gTotal = activeGroup.total ?? 0}
      {@const gSales = activeGroup.sales ?? 0}
      {@const gByStock = activeGroup.by_stock ?? {}}
      <div class="stat-card highlighted group-card-label">
        <div class="stat-label">GROUP · {activeGroup.name.slice(0, 22)}</div>
        <div class="stat-total-row">
          <span class="stat-total">${gTotal.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
        </div>
        <div class="stat-count">↓{gSales.toLocaleString()} sales</div>
      </div>
      {#each Object.entries(gByStock) as [sname, d]}
        <div class="stat-card">
          <div class="stat-label" style="color:{STOCK_COLORS[sname]||'#888'}">{STOCK_ABBR[sname]||sname}</div>
          <div class="stat-total-row">
            <span class="stat-total">${d.total.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
          </div>
          <div class="stat-count">↓{d.count.toLocaleString()}</div>
        </div>
      {/each}
    {:else}
      <!-- Normal mode: time-period breakdown -->
      {#each [
        { label: 'TODAY',    key: 'today' },
        { label: 'WEEK',     key: 'week'  },
        { label: 'MONTH',    key: 'month' },
        { label: 'YEAR',     key: 'year'  },
        { label: 'ALL-TIME', key: 'all'   },
      ] as card}
        {@const s = statFor(card.key)}
        {@const delta = deltaFor(card.key)}
        <button
          class="stat-card {$currentPeriod === PERIOD_MAP[card.key] ? 'highlighted' : ''}"
          onclick={() => clickStatCard(card.key)}>
          <div class="stat-label">
            {card.label}
            {#if downloadsStock !== 'All'}
              <span class="stat-stock-tag">· {STOCK_ABBR[downloadsStock] || downloadsStock}</span>
            {/if}
          </div>
          <div class="stat-total-row">
            <span class="stat-total">${(s.total ?? 0).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
            {#if delta > 0.005}
              <span class="stat-delta pos">+${delta.toFixed(2)}</span>
            {/if}
          </div>
          <div class="stat-count">↓{(s.count ?? 0).toLocaleString()}</div>
          {#if downloadsStock === 'All' && deltasMatrix[card.key]}
            <div class="stat-mini-stocks">
              {#each Object.entries(deltasMatrix[card.key]) as [name, val]}
                {#if name !== 'All'}
                  <span class="mini-stock" style="color:{STOCK_COLORS[name]||'#888'}">
                    {STOCK_ABBR[name]||name} +${Number(val).toFixed(2)}
                  </span>
                {/if}
              {/each}
            </div>
          {/if}
        </button>
      {/each}
    {/if}
  </div>

  <!-- Nav tabs -->
  <nav class="tab-bar">
    <div class="tab-bar-inner" bind:this={tabBarInner}>
      <span class="tab-slider {_sliderReady ? 'animated' : ''}" style="left:{tabSlider.left}px;width:{tabSlider.width}px"></span>
      {#each TABS as tab, i}
        <button class="tab-btn {activeTab === i ? 'active' : ''}" onclick={() => switchTab(i)}>
          {#if tab.emoji}<span class="tab-emoji">{tab.emoji}</span>
          {:else if tab.Icon}<tab.Icon size={14} strokeWidth={1.8} />{/if}
          {tab.label}
        </button>
      {/each}
    </div>
  </nav>

  <!-- Content -->
  <main class="tab-content">
    {#if activeTab === 0}
      <div class="tab-slide"
        in:fly={{ x: tabDir * -28, duration: 170, easing: cubicOut }}
        out:fly={{ x: tabDir * 28, duration: 130, easing: cubicIn }}>
        <Downloads
          onRefresh={loadStats}
          onStockChange={onDownloadsStockChange} />
      </div>
    {:else if activeTab === 1}
      <div class="tab-slide"
        in:fly={{ x: tabDir * -28, duration: 170, easing: cubicOut }}
        out:fly={{ x: tabDir * 28, duration: 130, easing: cubicIn }}>
        <BestSellers onStockChange={onDownloadsStockChange} />
      </div>
    {:else if activeTab === 2}
      <div class="tab-slide"
        in:fly={{ x: tabDir * -28, duration: 170, easing: cubicOut }}
        out:fly={{ x: tabDir * 28, duration: 130, easing: cubicIn }}>
        <Groups
          onGroupSelect={(/** @type {any} */ g) => activeGroup = g}
          onGroupDeselect={() => activeGroup = null} />
      </div>
    {:else if activeTab === 3}
      <div class="tab-slide"
        in:fly={{ x: tabDir * -28, duration: 170, easing: cubicOut }}
        out:fly={{ x: tabDir * 28, duration: 130, easing: cubicIn }}>
        <Browser onSyncDone={loadStats} />
      </div>
    {/if}
  </main>
</div>

{#if showColorSettings}
  <StockColorSettings
    stocks={Object.keys(STOCK_COLORS)}
    onClose={() => showColorSettings = false} />
{/if}

<style>
  /* ── macOS design tokens ─────────────────────────────── */
  :global(:root) {
    --bg:            #1c1c1e;
    --bg2:           #2c2c2e;
    --bg3:           #3a3a3c;
    --bg4:           #48484a;
    --glass:         rgba(60,62,72,0.22);
    --glass2:        rgba(28,30,38,0.34);
    --glass-border:  rgba(255,255,255,0.22);
    /* icy: bright top specular + chromatic edges (cyan left / magenta right) */
    --glass-shine:   inset 0 1.5px 0 rgba(255,255,255,0.42), inset 0 -1px 0 rgba(0,0,0,0.28);
    --refract:       inset 1.5px 1px 0 rgba(120,200,255,0.22), inset -1.5px -1px 0 rgba(255,150,210,0.16);
    --label:         #ffffff;
    --label2:        rgba(235,235,245,0.60);
    --label3:        rgba(235,235,245,0.30);
    --accent:        #0a84ff;
    --accent-dim:    rgba(10,132,255,0.18);
    --green:         #30d158;
    --red:           #ff453a;
    --orange:        #ff9f0a;
    --sep:           rgba(255,255,255,0.09);
    --shadow:        0 2px 4px rgba(0,0,0,0.4), 0 8px 24px rgba(0,0,0,0.6), 0 20px 60px rgba(0,0,0,0.5);
    --shadow-sm:     0 1px 3px rgba(0,0,0,0.3), 0 4px 16px rgba(0,0,0,0.4);
    --radius:        16px;
    --radius-sm:     11px;
    --radius-xs:     7px;
    --radius-pill:   999px;
    --blur:          blur(14px) saturate(180%) brightness(1.08);
    /* wet-ice gloss: diagonal specular sheen layered over the translucent fill */
    --gloss:         linear-gradient(150deg, rgba(255,255,255,0.22) 0%, rgba(255,255,255,0.05) 22%, rgba(255,255,255,0) 46%, rgba(255,255,255,0.04) 100%);
    --bg-img:        linear-gradient(180deg, rgba(18,20,28,0.22), rgba(12,14,20,0.42)), url('/bg-dark.jpg');
    --glass-edge:    linear-gradient(140deg, rgba(255,255,255,0.45) 0%, rgba(255,255,255,0.04) 30%, rgba(255,255,255,0.0) 55%, rgba(255,255,255,0.10) 100%);
    --sel-bg:        rgba(10,132,255,0.32);
    --sel-bg-solid:  rgba(10,132,255,0.52);
    --sel-border:    rgba(10,132,255,0.55);
    --sel-fg:        #ffffff;
    --sel-glow:      0 2px 10px rgba(10,132,255,0.08);
  }
  :global(body.light) {
    --bg:            #f2f2f7;
    --bg2:           #ffffff;
    --bg3:           #e5e5ea;
    --bg4:           #d1d1d6;
    --glass:         rgba(255,255,255,0.28);
    --glass2:        rgba(250,250,253,0.40);
    --glass-border:  rgba(255,255,255,0.7);
    --glass-shine:   inset 0 1.5px 0 rgba(255,255,255,1), inset 0 -1px 0 rgba(0,0,0,0.06);
    --refract:       inset 1.5px 1px 0 rgba(150,210,255,0.4), inset -1.5px -1px 0 rgba(255,180,220,0.3);
    --label:         #000000;
    --label2:        rgba(60,60,67,0.60);
    --label3:        rgba(60,60,67,0.30);
    --accent:        #007aff;
    --accent-dim:    rgba(0,122,255,0.12);
    --green:         #34c759;
    --red:           #ff3b30;
    --orange:        #ff9500;
    --sep:           rgba(0,0,0,0.08);
    --shadow:        0 2px 4px rgba(0,0,0,0.10), 0 8px 24px rgba(0,0,0,0.14), 0 20px 60px rgba(0,0,0,0.10);
    --shadow-sm:     0 1px 3px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.10);
    --sel-bg:        rgba(60,60,67,0.15);
    --sel-bg-solid:  rgba(60,60,67,0.30);
    --sel-border:    rgba(60,60,67,0.30);
    --sel-fg:        #000000;
    --sel-glow:      0 1px 3px rgba(0,0,0,0.14);
    --blur:          blur(14px) saturate(170%) brightness(1.04);
    --gloss:         linear-gradient(150deg, rgba(255,255,255,0.5) 0%, rgba(255,255,255,0.12) 22%, rgba(255,255,255,0) 46%, rgba(255,255,255,0.1) 100%);
    --bg-img:        linear-gradient(180deg, rgba(255,255,255,0.26), rgba(255,255,255,0.42)), url('/bg-light.jpg');
    --glass-edge:    linear-gradient(140deg, rgba(255,255,255,0.85) 0%, rgba(255,255,255,0.2) 28%, rgba(255,255,255,0.0) 55%, rgba(255,255,255,0.35) 100%);
  }

  /* macOS native glass: body transparent → window's NSVisualEffectView shows through */
  :global(html.vibrancy), :global(body.vibrancy) {
    background: transparent !important;
  }

  :global(*) { box-sizing: border-box; margin: 0; padding: 0; }
  /* SVG icons sit on text baseline by default — force block so flex align-items:center works */
  :global(button svg, a svg, span svg) { display: block; flex-shrink: 0; }
  :global(body) {
    background-color: var(--bg);
    background-image: var(--bg-img);
    background-size: cover;
    background-position: center;
    background-attachment: fixed;
    color: var(--label);
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', sans-serif;
    font-size: 13px; overflow: hidden; user-select: none;
    -webkit-font-smoothing: antialiased;
  }

  /* grain overlay — дізерить gradient banding */
  :global(body)::after {
    content: '';
    position: fixed; inset: 0; z-index: 9999; pointer-events: none;
    opacity: 0.055;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='300' height='300'%3E%3Cfilter id='g'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.75' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='300' height='300' filter='url(%23g)'/%3E%3C/svg%3E");
    background-size: 200px 200px;
    background-repeat: repeat;
  }
  :global(body.light)::after { opacity: 0.022; }

  :global(::-webkit-scrollbar) { width: 6px; }
  :global(::-webkit-scrollbar-track) { background: transparent; }
  :global(::-webkit-scrollbar-thumb) { background: var(--bg4); border-radius: 3px; }
  :global(::-webkit-scrollbar-thumb:hover) { background: var(--label3); }

  /* ── layout ─────────────────────────────────────────── */
  .app { display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

  /* ── topbar ─────────────────────────────────────────── */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 20px 8px; flex-shrink: 0;
    background: var(--gloss), var(--glass2);
    backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border-bottom: 1px solid var(--sep);
    border-radius: 0 0 var(--radius) var(--radius);
    box-shadow: var(--glass-shine), var(--refract), 0 2px 12px rgba(0,0,0,0.3);
  }
  .app-title {
    font-size: 15px; font-weight: 700; letter-spacing: -0.3px;
    background: linear-gradient(135deg, var(--accent), #5ec5ff);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }
  .topbar-right { display: flex; gap: 6px; }
  .icon-btn {
    background: var(--gloss), var(--glass); border: 1px solid var(--glass-border);
    color: var(--label2); border-radius: var(--radius-sm);
    width: 30px; height: 30px; cursor: pointer; font-size: 14px;
    display: flex; align-items: center; justify-content: center;
    transition: transform .12s, color .15s, border-color .15s; backdrop-filter: var(--blur);
    box-shadow: var(--glass-shine), var(--refract), 0 1px 3px rgba(0,0,0,0.18);
  }
  .icon-btn:hover { color: var(--label); border-color: rgba(10,132,255,0.45); transform: translateY(-1px); }
  .icon-btn:active { transform: translateY(0) scale(.95); }

  /* ── stats row ───────────────────────────────────────── */
  .stats-row {
    display: flex; gap: 10px; padding: 10px 20px; flex-shrink: 0;
  }
  .stat-card {
    flex: 1;
    background: var(--gloss), var(--glass); border: 1px solid var(--glass-border);
    backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border-radius: var(--radius); padding: 11px 14px;
    cursor: pointer; transition: all 0.18s; text-align: left;
    font: inherit; color: inherit;
    box-shadow: var(--shadow-sm), var(--glass-shine), var(--refract);
  }
  .stat-card:hover { border-color: var(--accent); background: rgba(10,132,255,0.06); transform: translateY(-1px); box-shadow: var(--shadow), var(--glass-shine), var(--refract); }
  .stat-card.highlighted { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent), var(--shadow-sm), var(--glass-shine); }
  .group-card-label { cursor: default; }
  .group-card-label .stat-label { color: var(--accent); font-size: 8px; }

  .stat-label {
    font-size: 9px; font-weight: 700; color: var(--label3);
    letter-spacing: 0.08em; margin-bottom: 4px;
    display: flex; align-items: center; gap: 4px; text-transform: uppercase;
  }
  .stat-stock-tag { color: var(--accent); font-size: 9px; }
  .stat-total-row { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
  .stat-total { font-size: 18px; font-weight: 700; color: var(--label); letter-spacing: -0.5px; }
  .stat-delta { font-size: 11px; font-weight: 600; }
  .stat-delta.pos { color: var(--green); }
  .stat-delta.neg { color: var(--red); }
  .stat-count { font-size: 10px; color: var(--label2); margin-top: 2px; }
  .stat-mini-stocks { display: flex; gap: 6px; margin-top: 4px; flex-wrap: wrap; }
  .mini-stock { font-size: 9px; font-weight: 600; }

  /* ── tab bar (macOS segmented control style) ─────────── */
  .tab-bar {
    display: flex; padding: 0 20px 10px; justify-content: center; flex-shrink: 0;
  }
  .tab-bar-inner {
    position: relative;
    display: flex; gap: 0;
    background: var(--gloss), var(--glass); border: 1px solid var(--glass-border);
    backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border-radius: var(--radius-pill); padding: 3px;
    box-shadow: var(--shadow-sm), var(--glass-shine), inset 0 1px 3px rgba(0,0,0,0.18);
  }
  .tab-slider {
    position: absolute;
    top: 3px; bottom: 3px;
    background: var(--sel-bg-solid);
    border: 1px solid var(--sel-border);
    border-radius: var(--radius-pill);
  }
  .tab-slider.animated {
    transition: left 0.26s cubic-bezier(0.34,1.15,0.64,1), width 0.26s cubic-bezier(0.34,1.15,0.64,1);
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,0.22),
      inset 0 -1px 0 rgba(0,0,0,0.18),
      var(--sel-glow);
    pointer-events: none;
    z-index: 0;
  }
  .tab-btn {
    position: relative; z-index: 1;
    background: transparent; border: none; color: var(--label2);
    font-size: 12px; font-weight: 600; padding: 6px 18px;
    border-radius: var(--radius-pill); cursor: pointer; transition: color 0.18s;
    font-family: inherit; letter-spacing: -0.1px; white-space: nowrap;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .tab-emoji { font-size: 13px; line-height: 1; }
  .tab-btn:hover { color: var(--label); }
  .tab-btn.active { color: var(--sel-fg); font-weight: 700; }

  .tab-content { flex: 1; overflow: hidden; position: relative; }
  .tab-slide { position: absolute; inset: 0; padding: 0 20px 20px; display: flex; flex-direction: column; will-change: transform; transform: translateZ(0); }

  /* ── shared global utility classes ───────────────────── */

  /* Universal button style — pill + blue glass border */
  :global(.pill),
  :global(.btn),
  :global(.action-pill) {
    display: inline-flex; align-items: center; gap: 7px;
    background: var(--gloss), var(--glass);
    backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-pill);
    color: var(--label2); font-size: 12px; font-weight: 600;
    padding: 6px 16px; cursor: pointer; font-family: inherit;
    white-space: nowrap;
    box-shadow: var(--glass-shine), var(--refract), 0 1px 3px rgba(0,0,0,0.18);
    transition: transform .12s, border-color .15s, box-shadow .15s, color .15s;
  }
  :global(.pill:hover:not(:disabled)),
  :global(.btn:hover:not(:disabled)),
  :global(.action-pill:hover:not(:disabled)) {
    color: var(--label); border-color: rgba(10,132,255,0.45);
    transform: translateY(-1px);
    box-shadow: var(--glass-shine), var(--refract), 0 3px 10px rgba(10,132,255,0.18);
  }
  :global(.pill:active:not(:disabled)),
  :global(.btn:active:not(:disabled)),
  :global(.action-pill:active:not(:disabled)) { transform: translateY(0) scale(.97); }
  :global(.pill:disabled),
  :global(.btn:disabled),
  :global(.action-pill:disabled) { opacity: .45; cursor: not-allowed; }

  /* Active / filled state */
  :global(.pill.active),
  :global(.btn.primary),
  :global(.action-pill.active) {
    background: linear-gradient(160deg, rgba(120,200,255,.4), rgba(10,132,255,.32)), var(--sel-bg-solid);
    border-color: rgba(120,200,255,0.7); color: var(--sel-fg);
    box-shadow: var(--glass-shine), 0 0 0 1px rgba(10,132,255,.25), 0 2px 10px rgba(10,132,255,0.3);
  }

  /* Danger variant (Stop button etc.) */
  :global(.pill.danger) {
    background: rgba(255,69,58,0.10);
    border-color: rgba(255,69,58,0.42); color: #ff453a;
    box-shadow: 0 0 10px rgba(255,69,58,0.08);
  }
  :global(.pill.danger:hover) { background: rgba(255,69,58,0.18); color: #fff; }

  :global(.card) {
    background: var(--glass); border-radius: var(--radius); border: 1px solid var(--glass-border);
    box-shadow: var(--shadow-sm), var(--glass-shine);
  }
  :global(.card:hover) { border-color: var(--accent); }

  :global(.scroll-y) { overflow-y: auto; height: 100%; }

  /* ── Floating frosted toolbar + content that scrolls UNDER it ──
     Global pattern (Downloads, BestSellers, …). The panel is real glass:
     content slides beneath it (blurred by its backdrop-filter) and fades
     out at the panel's edge via a mask on the scroll canvas.
     Each tab sets --panel-h (panel height, px) on .glass-canvas. */
  :global(.glass-wrap) { position: relative; height: 100%; }
  :global(.glass-panel) {
    position: absolute; top: 0; left: 0; right: 0; z-index: 12;
    background: var(--gloss), var(--glass); border: 1px solid var(--glass-border);
    backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    border-radius: var(--radius); padding: 10px 12px;
    box-shadow: var(--shadow-sm), var(--glass-shine), var(--refract);
  }
  :global(.glass-canvas) {
    position: absolute; inset: 0; overflow-y: auto;
    contain: paint; will-change: scroll-position;
    padding-top: calc(var(--panel-h, 56px) + 12px);
    /* content fades out just as it slides under the panel's top edge */
    -webkit-mask-image: linear-gradient(to bottom,
      transparent 0,
      transparent calc(var(--panel-h, 56px) * 0.35),
      #000 calc(var(--panel-h, 56px) + 6px), #000 100%);
    mask-image: linear-gradient(to bottom,
      transparent 0,
      transparent calc(var(--panel-h, 56px) * 0.35),
      #000 calc(var(--panel-h, 56px) + 6px), #000 100%);
  }
  :global(.accent) { color: var(--accent); }
  :global(.dim) { color: var(--label2); }

  :global(input), :global(textarea) {
    background: var(--bg3); border: 1px solid var(--glass-border);
    color: var(--label); border-radius: var(--radius-sm);
    font-family: inherit; font-size: 12px;
    outline: none; transition: border-color 0.15s;
  }
  :global(input:focus), :global(textarea:focus) { border-color: var(--accent); }
</style>
