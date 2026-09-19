/*
 * Seed the component tables straight from the simulator's own level config,
 * so you can develop and test with a realistic park while the simulator is
 * closed.
 *
 *   node scripts/seed-from-level.js            # lvl1
 *   node scripts/seed-from-level.js lvl2
 *
 * At runtime the real source of truth is still the bootstrap sync against the
 * live API; this is a development convenience.
 */
import fs from 'node:fs';
import path from 'node:path';
import { upsertComponent } from '../src/state.js';
import { db } from '../src/db.js';

const level = process.argv[2] ?? 'lvl1';

const candidates = [
  path.resolve(
    process.cwd(),
    '..',
    'ParkingSimulator-win-x64',
    'ParkingSimulator-win-x64',
    'settings',
    `${level}.json`,
  ),
  path.resolve(process.cwd(), '..', 'settings', `${level}.json`),
  path.resolve(process.cwd(), `${level}.json`),
];

const file = candidates.find((p) => fs.existsSync(p));
if (!file) {
  console.error(`Could not find ${level}.json. Looked in:`);
  candidates.forEach((c) => console.error('  ' + c));
  process.exit(1);
}

const cfg = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^﻿/, ''));
console.log(`seeding from ${file}\n`);

let counts = { Park: 0, EntrySpot: 0, ExitSpot: 0, LeaveParking: 0, BarrierGate: 0, Light: 0, ExhaustFan: 0, Zone: 0 };

for (const s of cfg.ParkingSpots ?? []) {
  upsertComponent({
    name: s.Name,
    kind: s.Purpose,
    zone: s.ZoneParent || null,
    carType: s.CarType ?? null,
    broken: false,
    underMaintenance: false,
  });
  counts[s.Purpose] = (counts[s.Purpose] ?? 0) + 1;
}

for (const g of cfg.Gates ?? []) {
  upsertComponent({
    name: g.Name,
    kind: 'BarrierGate',
    zone: g.ZoneParent || null,
    state: g.State,
    broken: false,
    underMaintenance: false,
  });
  counts.BarrierGate += 1;
}

for (const l of cfg.Lights ?? []) {
  upsertComponent({
    name: l.Name,
    kind: 'Light',
    zone: l.ZoneParent || null,
    lightGroup: l.Group ?? null,
    isOn: !!l.IsOn,
  });
  counts.Light += 1;
}

for (const f of cfg.Exhausts ?? []) {
  upsertComponent({
    name: f.Name,
    kind: 'ExhaustFan',
    zone: f.ZoneParent || null,
    isOn: !!f.IsOn,
    broken: false,
    underMaintenance: false,
  });
  counts.ExhaustFan += 1;
}

for (const z of cfg.Zones ?? []) {
  db.prepare(
    `INSERT INTO zones(name, co_level, danger, updated_at) VALUES (?, 0, 'Safe', ?)
     ON CONFLICT(name) DO NOTHING`,
  ).run(z.Name, new Date().toISOString());
  counts.Zone += 1;
}

console.log('seeded:');
for (const [k, v] of Object.entries(counts)) if (v) console.log(`  ${k.padEnd(14)} ${v}`);

const free = db
  .prepare(
    `SELECT COUNT(*) AS n FROM components
     WHERE kind = 'Park' AND broken = 0 AND under_maintenance = 0 AND occupied_by IS NULL`,
  )
  .get().n;
console.log(`\nfree parking spots: ${free}`);
