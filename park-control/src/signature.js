import crypto from 'node:crypto';
import { db, setMeta } from './db.js';
import { config } from './config.js';

/*
 * The docs describe the signature as: drop `Signature`, sort the remaining
 * field NAMES alphabetically, join their VALUES with "|", hash, compare.
 *
 * That recipe does not reproduce the signatures printed in the same document.
 * The worked example there includes a `RealDateTime` field that the sample
 * payloads omit, so the published samples are almost certainly incomplete and
 * cannot be verified offline.
 *
 * Rather than hardcode a guess, we evaluate a family of plausible recipes
 * against every live event and tally which one actually matches. Run
 * `npm run sig` after a few minutes of traffic to see the winner, then pin it
 * via SIGNATURE_RECIPE and flip SIGNATURE_MODE to `enforce`.
 */

const ALGOS = ['md5', 'sha1', 'sha256'];
const SEPARATORS = { pipe: '|', none: '', dash: '-', colon: ':' };
const ORDERS = {
  alpha: (keys) => [...keys].sort(),                                  // ordinal, uppercase first
  alphaci: (keys) => [...keys].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase())),
  insertion: (keys) => [...keys],                                     // JSON document order
};

/** Values are stringified the way they appeared on the wire where possible. */
function valueOf(raw, key) {
  const v = raw[key];
  if (v === null || v === undefined) return '';
  if (typeof v === 'number') {
    // Preserve the literal text from the JSON body so 63.564693 does not
    // become 63.564693000000004 or similar.
    return String(v);
  }
  return String(v);
}

function buildCandidates(payload) {
  const keys = Object.keys(payload).filter((k) => k !== 'Signature');
  const out = [];
  for (const [orderName, orderFn] of Object.entries(ORDERS)) {
    const ordered = orderFn(keys);
    for (const [sepName, sep] of Object.entries(SEPARATORS)) {
      const joined = ordered.map((k) => valueOf(payload, k)).join(sep);
      for (const algo of ALGOS) {
        out.push({
          recipe: `${algo}:${sepName}:${orderName}`,
          digest: crypto.createHash(algo).update(joined, 'utf8').digest('hex'),
          input: joined,
        });
      }
    }
  }
  return out;
}

const bumpTrial = db.prepare(
  `INSERT INTO signature_trials(recipe, matches, attempts) VALUES (?, ?, 1)
   ON CONFLICT(recipe) DO UPDATE SET
     matches  = matches  + excluded.matches,
     attempts = attempts + 1`,
);

/**
 * Verify a payload's signature.
 *
 * In `observe` mode nothing is ever rejected; we simply learn which recipe
 * matches. In `enforce` mode a pinned recipe must match.
 *
 * @returns {{ok: boolean|null, matched: string[], enforced: boolean}}
 *   ok === null means "no signature present to check".
 */
export function verifySignature(payload) {
  const provided = payload?.Signature;
  if (!provided) return { ok: null, matched: [], enforced: false };

  const candidates = buildCandidates(payload);
  const matched = [];

  for (const c of candidates) {
    const hit = c.digest.toLowerCase() === String(provided).toLowerCase();
    if (hit) matched.push(c.recipe);
    bumpTrial.run(c.recipe, hit ? 1 : 0);
  }

  if (matched.length > 0) setMeta('signature_last_match', matched.join(','));

  const pinned = config.signature.recipe;
  if (config.signature.mode === 'enforce') {
    if (!pinned) {
      // Refuse to silently accept everything when asked to enforce.
      return { ok: matched.length > 0, matched, enforced: true };
    }
    return { ok: matched.includes(pinned), matched, enforced: true };
  }

  return { ok: matched.length > 0, matched, enforced: false };
}

/** Recipes ranked by how often they reproduced the server's signature. */
export function signatureReport() {
  return db
    .prepare(
      `SELECT recipe, matches, attempts,
              ROUND(100.0 * matches / NULLIF(attempts, 0), 2) AS pct
       FROM signature_trials
       WHERE matches > 0
       ORDER BY matches DESC, recipe ASC`,
    )
    .all();
}

/** Total events we have attempted to verify, for context in the report. */
export function signatureAttempts() {
  const row = db.prepare('SELECT MAX(attempts) AS n FROM signature_trials').get();
  return row?.n ?? 0;
}
