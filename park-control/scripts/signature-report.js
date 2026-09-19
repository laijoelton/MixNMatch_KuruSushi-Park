/*
 * Which signature recipe does the simulator actually use?
 *
 * Run this after the listener has taken a few minutes of live traffic:
 *   npm run sig
 *
 * A recipe at 100% across a few hundred events is your answer. Pin it in .env
 * as SIGNATURE_RECIPE and switch SIGNATURE_MODE to `enforce`.
 */
import { signatureReport, signatureAttempts } from '../src/signature.js';
import { db } from '../src/db.js';

const attempts = signatureAttempts();
const rows = signatureReport();

console.log(`\nEvents evaluated: ${attempts}\n`);

if (attempts === 0) {
  console.log('No events seen yet. Start the listener and let the simulator run.');
  process.exit(0);
}

if (rows.length === 0) {
  console.log('No candidate recipe matched any event.');
  console.log('\nThe signature likely includes a field or secret we cannot see.');
  console.log('Next steps:');
  console.log('  1. Inspect a raw payload for fields the docs omit:');
  console.log('     sqlite3 data/park.db "SELECT payload FROM events LIMIT 1;"');
  console.log('  2. Ask the organisers for the algorithm and the exact field set.');
  console.log('  3. Keep SIGNATURE_MODE=observe until resolved - do not drop events.');

  const sample = db.prepare('SELECT payload FROM events LIMIT 1').get();
  if (sample) {
    console.log('\nSample payload keys:');
    console.log('  ' + Object.keys(JSON.parse(sample.payload)).join(', '));
  }
  process.exit(0);
}

console.log('recipe (algo:separator:key-order)        matches  attempts   rate');
console.log('-'.repeat(68));
for (const r of rows) {
  console.log(
    `${r.recipe.padEnd(40)} ${String(r.matches).padStart(7)} ${String(r.attempts).padStart(9)} ${String(r.pct).padStart(6)}%`,
  );
}

const winner = rows[0];
if (winner.pct === 100) {
  console.log(`\nPin this in .env:\n  SIGNATURE_RECIPE=${winner.recipe}\n  SIGNATURE_MODE=enforce`);
} else {
  console.log(`\nBest so far is ${winner.recipe} at ${winner.pct}% - collect more events before pinning.`);
}
