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
  .pill {
    padding: 4px 10px; border-radius: 12px;
    border: 1px solid var(--glass-border); background: var(--glass);
    color: var(--label2); font-size: 12px; font-weight: 600;
    cursor: pointer; font-family: inherit; white-space: nowrap;
  }
  .pill:hover { color: var(--label); border-color: rgba(10,132,255,0.4); }
  .pill.active { background: rgba(10,132,255,0.18); color: var(--label); border-color: rgba(10,132,255,0.7); }
</style>
