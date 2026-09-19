import { db, now } from './db.js';
import { api, normalisePlate } from './api.js';
import { config } from './config.js';

/** Car lifecycle. Kept explicit so illegal transitions are easy to spot. */
export const CarState = {
  ARRIVED: 'ARRIVED',       // seen at an entry spot, no spot assigned yet
  ASSIGNED: 'ASSIGNED',     // told to drive to a spot, not parked yet
  PARKED: 'PARKED',         // sitting in the spot, clock running
  LEAVING: 'LEAVING',       // left the spot, heading for an exit
  AT_EXIT: 'AT_EXIT',       // at the exit spot, awaiting charge
  CHARGED: 'CHARGED',       // we called /charge, awaiting payment_made
  PAID: 'PAID',             // payment validated, safe to release
  RELEASED: 'RELEASED',     // told to leavepark
  GONE: 'GONE',             // confirmed out of the facility
};

// ---------------------------------------------------------------- components

const upsertComponentStmt = db.prepare(
  `INSERT INTO components
     (name, kind, zone, state, car_type, light_group, is_on, broken,
      under_maintenance, updated_at)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
   ON CONFLICT(name) DO UPDATE SET
     kind              = excluded.kind,
     zone              = COALESCE(excluded.zone, components.zone),
     state             = COALESCE(excluded.state, components.state),
     car_type          = COALESCE(excluded.car_type, components.car_type),
     light_group       = COALESCE(excluded.light_group, components.light_group),
     is_on             = COALESCE(excluded.is_on, components.is_on),
     broken            = excluded.broken,
     under_maintenance = excluded.under_maintenance,
     updated_at        = excluded.updated_at`,
);

export function upsertComponent(c) {
  upsertComponentStmt.run(
    c.name,
    c.kind,
    c.zone ?? null,
    c.state ?? null,
    c.carType ?? null,
    c.lightGroup ?? null,
    c.isOn === undefined ? null : c.isOn ? 1 : 0,
    c.broken ? 1 : 0,
    c.underMaintenance ? 1 : 0,
    now(),
  );
}

export const getComponent = (name) =>
  db.prepare('SELECT * FROM components WHERE name = ?').get(name);

export function setComponentFlags(name, { broken, underMaintenance, state, isOn }) {
  const existing = getComponent(name);
  if (!existing) return;
  db.prepare(
    `UPDATE components SET
       broken            = COALESCE(?, broken),
       under_maintenance = COALESCE(?, under_maintenance),
       state             = COALESCE(?, state),
       is_on             = COALESCE(?, is_on),
       last_broken_at    = CASE WHEN ? = 1 THEN ? ELSE last_broken_at END,
       last_fixed_at     = CASE WHEN ? = 1 THEN ? ELSE last_fixed_at END,
       updated_at        = ?
     WHERE name = ?`,
  ).run(
    broken === undefined ? null : broken ? 1 : 0,
    underMaintenance === undefined ? null : underMaintenance ? 1 : 0,
    state ?? null,
    isOn === undefined ? null : isOn ? 1 : 0,
    broken ? 1 : 0,
    now(),
    broken === false ? 1 : 0,
    now(),
    now(),
    name,
  );
}

export function bumpUsage(name) {
  db.prepare('UPDATE components SET usage_count = usage_count + 1 WHERE name = ?').run(name);
}

// -------------------------------------------------------------- spot booking

/**
 * Pick a spot for a car.
 *
 * Reserving at assignment time (occupied_by set before the car arrives) is what
 * prevents Penalty_SendCarToOccupiedSpot when two cars arrive back to back --
 * the sensor CarIn event for car A lands well after we have already had to
 * answer the arrival of car B.
 */
export function allocateSpot(plate, carType) {
  const wanted = carType === 'Electric' ? 'Electric'
    : carType === 'Accessible' ? 'Accessible'
    : 'Any';

  const pick = (typeClause, params) =>
    db
      .prepare(
        `SELECT name FROM components
         WHERE kind = 'Park'
           AND broken = 0
           AND under_maintenance = 0
           AND occupied_by IS NULL
           ${typeClause}
         ORDER BY usage_count ASC, name ASC
         LIMIT 1`,
      )
      .get(...params);

  // Prefer an exact type match; fall back to a generic spot only for normal cars.
  let row = pick('AND car_type = ?', [wanted]);
  if (!row && wanted === 'Any') row = pick("AND (car_type IS NULL OR car_type = 'Any')", []);
  if (!row) return null;

  db.prepare('UPDATE components SET occupied_by = ?, updated_at = ? WHERE name = ?').run(
    normalisePlate(plate),
    now(),
    row.name,
  );
  return row.name;
}

export function releaseSpot(spotName) {
  if (!spotName) return;
  db.prepare('UPDATE components SET occupied_by = NULL, updated_at = ? WHERE name = ?').run(
    now(),
    spotName,
  );
}

export const freeSpotCount = () =>
  db
    .prepare(
      `SELECT COUNT(*) AS n FROM components
       WHERE kind = 'Park' AND broken = 0 AND under_maintenance = 0 AND occupied_by IS NULL`,
    )
    .get().n;

// ---------------------------------------------------------------------- cars

export const getCar = (plate) =>
  db.prepare('SELECT * FROM cars WHERE plate = ?').get(normalisePlate(plate));

export function upsertCar(plate, fields) {
  const p = normalisePlate(plate);
  const existing = getCar(p);

  if (!existing) {
    db.prepare(
      `INSERT INTO cars (plate, car_type, state, planned_minutes, entry_spot, arrived_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      p,
      fields.carType ?? null,
      fields.state ?? CarState.ARRIVED,
      fields.plannedMinutes ?? null,
      fields.entrySpot ?? null,
      fields.arrivedAt ?? now(),
      now(),
    );
    return getCar(p);
  }

  const map = {
    carType: 'car_type',
    state: 'state',
    plannedMinutes: 'planned_minutes',
    assignedSpot: 'assigned_spot',
    entrySpot: 'entry_spot',
    exitSpot: 'exit_spot',
    arrivedAt: 'arrived_at',
    parkedAt: 'parked_at',
    leftSpotAt: 'left_spot_at',
    atExitAt: 'at_exit_at',
    chargedAt: 'charged_at',
    releasedAt: 'released_at',
    parkingCost: 'parking_cost',
    chargingCost: 'charging_cost',
    paidAmount: 'paid_amount',
    paymentOk: 'payment_ok',
  };

  const sets = [];
  const vals = [];
  for (const [key, column] of Object.entries(map)) {
    if (fields[key] !== undefined) {
      sets.push(`${column} = ?`);
      vals.push(typeof fields[key] === 'boolean' ? (fields[key] ? 1 : 0) : fields[key]);
    }
  }
  if (sets.length === 0) return existing;

  sets.push('updated_at = ?');
  vals.push(now(), p);
  db.prepare(`UPDATE cars SET ${sets.join(', ')} WHERE plate = ?`).run(...vals);
  return getCar(p);
}

export function archiveSession(plate) {
  const car = getCar(plate);
  if (!car) return;
  const minutes =
    car.parked_at && car.left_spot_at
      ? (new Date(car.left_spot_at) - new Date(car.parked_at)) / 60000
      : null;

  db.prepare(
    `INSERT INTO sessions
       (plate, car_type, spot, parked_at, left_spot_at, minutes,
        parking_cost, charging_cost, paid_amount, payment_ok, released_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    car.plate,
    car.car_type,
    car.assigned_spot,
    car.parked_at,
    car.left_spot_at,
    minutes,
    car.parking_cost,
    car.charging_cost,
    car.paid_amount,
    car.payment_ok,
    now(),
  );
  db.prepare('DELETE FROM cars WHERE plate = ?').run(car.plate);
}

// ------------------------------------------------------------------- billing

/**
 * Compute what a car owes.
 *
 * WARNING -- the documentation contradicts itself here. One page says
 * "Charging cost: 1 per each minute, multiply by 2 if electric"; another says
 * "parking cost = total minutes spent parking, multiplied by 2 if car is
 * electric". Meanwhile the API takes parkingCost AND chargingCost separately,
 * and there is a Penalty_ChargeCarForNoElectricityUsed for billing electricity
 * to a car that used none.
 *
 * The reading encoded here: an electric car pays `minutes` of parking plus
 * `minutes` of electricity (2x total); everything else pays `minutes` with
 * chargingCost = 0. VERIFY THIS against a real car before trusting it --
 * Penalty_CarChargedIncorrectParkingAmount is the most expensive thing to get
 * wrong repeatedly.
 */
export function computeCharge(car) {
  if (!car?.parked_at) return { parkingCost: 0, chargingCost: 0, minutes: 0 };

  const end = car.left_spot_at ? new Date(car.left_spot_at) : new Date();
  const minutes = Math.max(0, (end - new Date(car.parked_at)) / 60000);
  const billable = Math.ceil(minutes);
  const isElectric = car.car_type === 'Electric';

  const base = billable * config.billing.ratePerMinute;

  if (!isElectric) return { parkingCost: base, chargingCost: 0, minutes: billable };

  if (config.billing.electricSplitCharging) {
    return {
      parkingCost: base,
      chargingCost: base * (config.billing.electricMultiplier - 1),
      minutes: billable,
    };
  }
  return {
    parkingCost: base * config.billing.electricMultiplier,
    chargingCost: 0,
    minutes: billable,
  };
}

// ----------------------------------------------------------------- bootstrap

/**
 * One-shot sync from the simulator. The docs are explicit that the list
 * endpoints carry a simulated operational cost, so this runs at startup and
 * after a crash -- never on a timer.
 */
export async function bootstrapFromSimulator() {
  const summary = { spots: 0, gates: 0, lights: 0, fans: 0, zones: 0, alarms: 0 };

  const spots = await api.listParkingSpots();
  for (const s of spots ?? []) {
    upsertComponent({
      name: s.name,
      kind: s.purpose ?? 'Park',
      zone: s.zoneParent || null,
      carType: s.parkingForCarType ?? null,
      broken: s.broken,
      underMaintenance: s.isUnderMaintenance,
    });
    // Reflect any car the simulator already sees in the spot.
    const detected = Array.isArray(s.detectedCars) ? s.detectedCars : [];
    if (detected.length > 0) {
      db.prepare('UPDATE components SET occupied_by = ? WHERE name = ?').run(
        normalisePlate(detected[0]?.plate ?? detected[0]),
        s.name,
      );
    }
    summary.spots += 1;
  }

  for (const g of (await api.listBarriers()) ?? []) {
    upsertComponent({
      name: g.name,
      kind: 'BarrierGate',
      zone: g.zoneParent || null,
      state: g.state,
      broken: g.broken,
      underMaintenance: g.isUnderMaintenance,
    });
    summary.gates += 1;
  }

  for (const l of (await api.listLights()) ?? []) {
    upsertComponent({
      name: l.name,
      kind: 'Light',
      zone: l.zoneParent || null,
      lightGroup: l.group ?? null,
      isOn: l.isOn,
    });
    summary.lights += 1;
  }

  for (const f of (await api.listExhaustFans()) ?? []) {
    upsertComponent({
      name: f.name,
      kind: 'ExhaustFan',
      zone: f.zoneParent || null,
      isOn: f.isOn,
      broken: f.broken,
      underMaintenance: f.isUnderMaintenance,
    });
    summary.fans += 1;
  }

  for (const z of (await api.listZones()) ?? []) {
    db.prepare(
      `INSERT INTO zones(name, co_level, danger, updated_at) VALUES (?, ?, ?, ?)
       ON CONFLICT(name) DO UPDATE SET
         co_level = excluded.co_level,
         danger = excluded.danger,
         updated_at = excluded.updated_at`,
    ).run(z.name, z.gasCarbonMonoxideLevel ?? 0, z.risk ?? null, now());
    summary.zones += 1;
  }

  for (const a of (await api.listAlarms()) ?? []) {
    setComponentFlags(a.name, { broken: true });
    summary.alarms += 1;
  }

  return summary;
}
