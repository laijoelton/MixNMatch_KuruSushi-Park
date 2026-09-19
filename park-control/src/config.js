import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// Minimal .env loader so we stay at one npm dependency.
function loadEnv() {
  const file = path.join(root, '.env');
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim();
    if (!(key in process.env)) process.env[key] = value;
  }
}
loadEnv();

const bool = (v, fallback) => (v === undefined ? fallback : /^(1|true|yes|on)$/i.test(v));
const num = (v, fallback) => (v === undefined || v === '' ? fallback : Number(v));

export const config = {
  root,
  dataDir: path.join(root, 'data'),
  dbPath: path.join(root, 'data', 'park.db'),

  sim: {
    baseUrl: (process.env.SIM_BASE_URL ?? 'http://127.0.0.1:9898').replace(/\/+$/, ''),
    email: process.env.SIM_EMAIL ?? 'admin',
    password: process.env.SIM_PASSWORD ?? 'admin',
  },

  port: num(process.env.PORT, 4000),

  signature: {
    mode: process.env.SIGNATURE_MODE ?? 'observe', // observe | enforce
    recipe: process.env.SIGNATURE_RECIPE || null,
  },

  billing: {
    ratePerMinute: num(process.env.RATE_PER_MINUTE, 1),
    electricMultiplier: num(process.env.ELECTRIC_MULTIPLIER, 2),
    electricSplitCharging: bool(process.env.ELECTRIC_SPLIT_CHARGING, true),
  },

  gates: {
    entry: process.env.DEFAULT_ENTRY_GATE ?? 'gateA',
    exit: process.env.DEFAULT_EXIT_GATE ?? 'gateB',
  },

  autopilot: bool(process.env.AUTOPILOT, false),
};

fs.mkdirSync(config.dataDir, { recursive: true });
