// Dev (Vite at :1420): proxy forwards /api + /img → Flask :8000 — keep relative URLs
// Prod build (.app or `vite build`): no proxy, hit Flask directly via absolute URL.
// Use Vite's compile-time PROD flag so the value is baked in correctly — runtime
// `window` checks fail during SSR prerender and get inlined as `false` → ''.
export const API_BASE = import.meta.env.PROD ? 'http://localhost:8000' : '';
const BASE = API_BASE;

/** @param {string} path @param {Record<string,any>} [params] */
async function get(path, params = {}) {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null);
  const qs = entries.length ? '?' + new URLSearchParams(entries) : '';
  const r = await fetch(BASE + path + qs);
  if (!r.ok) throw new Error(`GET ${path} → ${r.status}`);
  return r.json();
}

/** @param {string} path @param {any} [body] */
async function post(path, body = {}) {
  const r = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!r.ok) throw new Error(`POST ${path} → ${r.status}`);
  return r.json();
}

export const api = {
  stats:           /** @param {any} [stock]  */ (stock)   => get('/api/stats', { stock }),
  feed:            /** @param {any} params   */ (params)  => get('/api/feed', params),
  sales:           /** @param {any} params   */ (params)  => get('/api/sales', params),
  groups:          ()                                     => get('/api/groups'),
  photoGroups:     ()                                     => get('/api/photo-groups'),
  savePhotoGroups: /** @param {any} data     */ (data)    => post('/api/photo-groups', data),
  matches:         ()                                     => get('/api/matches'),
  saveMatches:     /** @param {any} data     */ (data)    => post('/api/matches', data),
  syncStatus:      ()                                     => get('/api/sync/status'),
  imgUrl:          /** @param {any} aid      */ (aid)     => `${BASE}/img/cache/${aid}`,
};
