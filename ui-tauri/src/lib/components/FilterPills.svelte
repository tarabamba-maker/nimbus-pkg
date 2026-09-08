<script>
  /**
   * @typedef {Object} Props
   * @property {string} [label]            - label shown left of pills ("Period:", "Stock:", etc.)
   * @property {string[]} options          - list of option labels
   * @property {string} value              - currently selected option
   * @property {(v:string)=>void} onSelect - called when user picks one
   * @property {string} [activeSuffix]     - optional suffix appended to the active pill (e.g. " ↓")
   */

  /** @type {Props} */
  let { label = '', options, value, onSelect, activeSuffix = '' } = $props();
</script>

<div class="row">
  {#if label}<span class="label">{label}</span>{/if}
  {#each options as opt}
    <button class="pill {value === opt ? 'active' : ''}" onclick={() => onSelect(opt)}>
      {opt}{value === opt ? activeSuffix : ''}
    </button>
  {/each}
</div>

<style>
  .row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 4px 0; }
  .label { font-size: 12px; color: var(--label3); font-weight: 600; margin-right: 4px; }
  /* icy 3D pill — glossy fill + dual specular/refraction edges */
  .pill {
    padding: 5px 12px; border-radius: 999px;
    border: 1px solid var(--glass-border);
    background: var(--gloss), var(--glass);
    backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
    color: var(--label2); font-size: 12px; font-weight: 600;
    cursor: pointer; font-family: inherit; white-space: nowrap;
    box-shadow: var(--glass-shine), var(--refract), 0 1px 3px rgba(0,0,0,0.18);
    transition: transform .12s, border-color .15s, box-shadow .15s;
  }
  .pill:hover { color: var(--label); border-color: rgba(10,132,255,0.45); transform: translateY(-1px); }
  .pill:active { transform: translateY(0) scale(.97); }
  .pill.active {
    background: linear-gradient(160deg, rgba(120,200,255,.35), rgba(10,132,255,.28)), var(--glass);
    color: var(--label); border-color: rgba(120,200,255,0.7);
    box-shadow: var(--glass-shine), 0 0 0 1px rgba(10,132,255,.25), 0 2px 8px rgba(10,132,255,.3);
  }
</style>
