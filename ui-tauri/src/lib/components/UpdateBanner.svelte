<script>
  import { onMount } from 'svelte';
  import { Download, X } from 'lucide-svelte';

  let available = $state(/** @type {{ version:string, body?:string } | null} */ (null));
  let installing = $state(false);
  let progress   = $state(0);
  let totalBytes = $state(0);
  let dlBytes    = $state(0);
  let dismissed  = $state(false);
  let errorMsg   = $state('');

  /** Tauri-only: check, download, install update; restart when done. */
  async function checkForUpdate() {
    try {
      const { check } = await import('@tauri-apps/plugin-updater');
      const upd = await check();
      if (upd) {
        available = { version: upd.version, body: upd.body || '' };
        return upd;
      }
    } catch (e) {
      // Not running inside Tauri (e.g. dev in browser) or no network — silently ignore
      console.debug('[updater] check failed:', e);
    }
    return null;
  }

  async function applyUpdate() {
    if (installing) return;
    installing = true; errorMsg = '';
    try {
      const { check } = await import('@tauri-apps/plugin-updater');
      const { relaunch } = await import('@tauri-apps/plugin-process');
      const upd = await check();
      if (!upd) { installing = false; available = null; return; }
      await upd.downloadAndInstall((evt) => {
        if (evt.event === 'Started')   { totalBytes = evt.data.contentLength ?? 0; dlBytes = 0; }
        if (evt.event === 'Progress')  { dlBytes += evt.data.chunkLength ?? 0;
                                          progress = totalBytes ? dlBytes / totalBytes : 0; }
        if (evt.event === 'Finished')  { progress = 1; }
      });
      await relaunch();
    } catch (e) {
      errorMsg = String(e);
      installing = false;
    }
  }

  onMount(() => {
    // Check once on launch, then every 6 hours while app is open.
    checkForUpdate();
    const id = setInterval(checkForUpdate, 6 * 60 * 60 * 1000);
    return () => clearInterval(id);
  });
</script>

{#if available && !dismissed}
  <div class="banner">
    <div class="info">
      <Download size={13} strokeWidth={2} />
      <span class="title">Update available — v{available.version}</span>
      {#if installing && totalBytes > 0}
        <span class="progress">{Math.round(progress * 100)}%</span>
      {/if}
      {#if errorMsg}<span class="error">{errorMsg}</span>{/if}
    </div>
    <div class="actions">
      <button class="btn primary" onclick={applyUpdate} disabled={installing}>
        {installing ? 'Installing…' : 'Install & restart'}
      </button>
      <button class="dismiss" onclick={() => dismissed = true} title="Dismiss">
        <X size={13} strokeWidth={2} />
      </button>
    </div>
  </div>
{/if}

<style>
  .banner {
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px; padding: 8px 16px;
    background: rgba(10,132,255,0.12);
    border-bottom: 1px solid rgba(10,132,255,0.35);
    font-size: 12px; color: var(--label);
  }
  .info    { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .title   { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .progress { color: var(--accent); font-weight: 700; font-variant-numeric: tabular-nums; }
  .error   { color: var(--red); font-size: 11px; }
  .actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .dismiss {
    background: none; border: none; color: var(--label3); cursor: pointer;
    padding: 4px; display: flex; align-items: center; border-radius: 4px;
  }
  .dismiss:hover { color: var(--label); background: rgba(255,255,255,0.06); }
</style>
