import { API_BASE } from "./api.js";
import { writable } from 'svelte/store';

const DEFAULTS = {
  'Adobe Stock':   '#f97316',
  'Shutterstock':  '#e11d48',
  'Getty Images':  '#a855f7',
  'iStock':        '#6366f1',
  'iStockphoto':   '#6366f1',
  'Depositphotos': '#0061ff',
  'Envato':        '#81b441',
  'Freepik':       '#1273eb',
  '123RF':         '#e64a19',
  'PIXTA':         '#00b0a8',
  'Dreamstime':    '#8cc63f',
  'Alamy':         '#00a651',
};

export const stockColors = writable(/** @type {Record<string,string>} */ ({ ...DEFAULTS }));

export async function loadStockColors() {
  try {
    const c = await fetch(API_BASE + '/api/stock-colors').then(r => r.json());
    stockColors.set({ ...DEFAULTS, ...c });
  } catch {}
}

/** @param {string} stock @param {string} color */
export async function saveStockColor(stock, color) {
  stockColors.update(c => ({ ...c, [stock]: color }));
  await fetch(API_BASE + '/api/stock-colors', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ [stock]: color }),
  }).catch(() => {});
}

export const STOCK_ABBR = /** @type {Record<string,string>} */ ({
  'Adobe Stock': 'A', 'Shutterstock': 'S', 'Getty Images': 'G',
  'iStock': 'iS', 'iStockphoto': 'iS', 'Depositphotos': 'DP',
});
