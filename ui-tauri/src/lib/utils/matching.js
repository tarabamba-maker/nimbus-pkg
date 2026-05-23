// Pure helpers for cross-stock match maps.
// matches: { primary_id: [primary_id, sibling_id, ...], ... }

/** Remove an asset_id from any match group; collapse groups that become singletons. */
export function unlinkFromMatches(/** @type {string} */ aid, /** @type {Record<string,string[]>} */ matches) {
  const next = { ...matches };
  if (aid in next) delete next[aid];
  for (const [primary, members] of Object.entries(next)) {
    next[primary] = members.filter(id => id !== aid);
    if (next[primary].length <= 1) delete next[primary];
  }
  return next;
}

/** Link two asset_ids in the same match group. */
export function linkMatches(/** @type {string} */ a1, /** @type {string} */ a2,
                            /** @type {Record<string,string[]>} */ matches) {
  const next = { ...matches };
  if (a1 in next) {
    if (!next[a1].includes(a2)) next[a1] = [...next[a1], a2];
  } else if (a2 in next) {
    if (!next[a2].includes(a1)) next[a2] = [...next[a2], a1];
  } else {
    next[a1] = [a1, a2];
  }
  return next;
}

/** Return sibling ids for an asset (excluding itself), or null if not in any match. */
export function getSiblings(/** @type {string} */ aid, /** @type {Record<string,string[]>} */ matches) {
  if (aid in matches) return matches[aid].filter(id => id !== aid);
  for (const members of Object.values(matches)) {
    if (members.includes(aid)) return members.filter(id => id !== aid);
  }
  return null;
}
