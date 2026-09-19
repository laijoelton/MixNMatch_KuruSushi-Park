import { db, now } from './db.js';
import { config } from './config.js';
import { api, normalisePlate } from './api.js';
import {
  CarState,
  allocateSpot,
  archiveSession,
  bumpUsage,
  computeCharge,
  getCar,
  getComponent,
  releaseSpot,
  setComponentFlags,
  upsertCar,
  upsertComponent,
} from './state.js';

const log = (...args) => console.log(new Date().toISOString(), ...args);

/** Commands are no-ops until AUTOPILOT=true, so you can watch before acting. */
async function act(description, fn) {
  if (!config.autopilot) {
    log(`[dry-run] ${description}`);
    return null;
  }
  try {
    log(`[act] ${description}`);
    return await fn();
  } catch (err) {
    log(`[act:FAILED] ${description} -> ${err.message}`);
    return null;
  }
}

// ------------------------------------------------------------- car movements

async function onCarAtEntry(ev) {
  const plate = normalisePlate(ev.CarPlateNumber);
  const planned = Number(ev.PlannedParkingDurationInMinutes ?? 0);

  upsertCar(plate, {
    carType: ev.CarType ?? 'Normal',
    state: CarState.ARRIVED,
    plannedMinutes: Number.isFinite(planned) ? planned : null,
    entrySpot: ev.SpotName,
    arrivedAt: now(),
  });

  const spot = allocateSpot(plate, ev.CarType);
  if (!spot) {
    // Full. Sending the car away beats Penalty_CarLeftFromEntryBecauseNeglected,
    // which fires if we simply ignore it.
    log(`[full] no free spot for ${plate} (${ev.CarType}) - sending to leavepark`);
    await act(`car ${plate} -> leavepark (park full)`, () => api.carGoto(plate, 'leavepark'));
    upsertCar(plate, { state: CarState.RELEASED });
    return;
  }

  upsertCar(plate, { state: CarState.ASSIGNED, assignedSpot: spot });

  // Open the gate first, then dispatch. The gate_action Open webhook confirms it.
  await act(`open ${config.gates.entry} for ${plate}`, () => api.openGate(config.gates.entry));
  await act(`car ${plate} -> ${spot}`, () => api.carGoto(plate, spot));
}

async function onCarLeftEntry(ev) {
  const plate = normalisePlate(ev.CarPlateNumber);
  // Car is through; close behind it to keep the usage counter honest.
  await act(`close ${config.gates.entry} after ${plate}`, () =>
    api.closeGate(config.gates.entry),
  );
}

function onCarParked(ev) {
  const plate = normalisePlate(ev.CarPlateNumber);
  const planned = Number(ev.PlannedParkingDurationInMinutes ?? 0);
  const car = getCar(plate);

  // The entry event often reports planned duration as 0 and the real value
  // only appears here, so take whichever is larger.
  const plannedMinutes = Math.max(car?.planned_minutes ?? 0, Number.isFinite(planned) ? planned : 0);

  upsertCar(plate, {
    state: CarState.PARKED,
    assignedSpot: ev.SpotName,
    parkedAt: now(),
    plannedMinutes: plannedMinutes || null,
  });

  // Confirm the reservation matches reality; the sim is the source of truth.
  db.prepare('UPDATE components SET occupied_by = ?, updated_at = ? WHERE name = ?').run(
    plate,
    now(),
    ev.SpotName,
  );
  bumpUsage(ev.SpotName);

  if (car?.assigned_spot && car.assigned_spot !== ev.SpotName) {
    log(`[drift] ${plate} parked in ${ev.SpotName} but we reserved ${car.assigned_spot}`);
    releaseSpot(car.assigned_spot);
  }
}

function onCarLeftSpot(ev) {
  const plate = normalisePlate(ev.CarPlateNumber);
  upsertCar(plate, { state: CarState.LEAVING, leftSpotAt: now() });
  releaseSpot(ev.SpotName);
}

async function onCarAtExit(ev) {
  const plate = normalisePlate(ev.CarPlateNumber);
  let car = getCar(plate);

  if (!car) {
    log(`[orphan] ${plate} reached exit with no session on record`);
    upsertCar(plate, { state: CarState.AT_EXIT, exitSpot: ev.SpotName, atExitAt: now() });
    car = getCar(plate);
  }

  // Charging twice is its own penalty, so this guard is load-bearing.
  if (car.charged_at) {
    log(`[skip] ${plate} already charged at ${car.charged_at}`);
    return;
  }

  upsertCar(plate, { state: CarState.AT_EXIT, exitSpot: ev.SpotName, atExitAt: now() });
  car = getCar(plate);

  const { parkingCost, chargingCost, minutes } = computeCharge(car);
  log(`[bill] ${plate}: ${minutes}min -> parking=${parkingCost} charging=${chargingCost}`);

  await act(`charge ${plate} parking=${parkingCost} charging=${chargingCost}`, () =>
    api.charge(plate, parkingCost, chargingCost),
  );

  upsertCar(plate, {
    state: CarState.CHARGED,
    chargedAt: now(),
    parkingCost,
    chargingCost,
  });
}

function onCarExited(ev) {
  const plate = normalisePlate(ev.CarPlateNumber);
  upsertCar(plate, { state: CarState.GONE });
  archiveSession(plate);
}

async function handleCarSpotAction(ev) {
  const type = ev.SpotType;
  const dir = ev.Direction;

  if (type === 'EntrySpot' && dir === 'CarIn') return onCarAtEntry(ev);
  if (type === 'EntrySpot' && dir === 'CarOut') return onCarLeftEntry(ev);
  if (type === 'Park' && dir === 'CarIn') return onCarParked(ev);
  if (type === 'Park' && dir === 'CarOut') return onCarLeftSpot(ev);
  if (type === 'ExitSpot' && dir === 'CarIn') return onCarAtExit(ev);
  if (type === 'ExitSpot' && dir === 'CarOut') return onCarExited(ev);

  log(`[unhandled] car_spot_action ${type}/${dir}`);
}

// ---------------------------------------------------------------- payments

async function handlePaymentMade(ev) {
  const plate = normalisePlate(ev.CarPlateNumber);
  const amount = Number(ev.Amount);
  const car = getCar(plate);

  const expected = car ? (car.parking_cost ?? 0) + (car.charging_cost ?? 0) : null;

  // "Some cars will tweak the system and send fake payment" -- compare against
  // what we actually billed, never trust the reported Amount.
  const valid =
    expected !== null && Number.isFinite(amount) && Math.abs(amount - expected) < 0.005;

  db.prepare(
    `INSERT OR IGNORE INTO payments
       (event_id, plate, amount, expected, valid, reason, server_datetime)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
  ).run(ev.EventId, plate, amount, expected, valid ? 1 : 0, ev.Reason ?? null, ev.ServerDateTime ?? null);

  upsertCar(plate, { paidAmount: amount, paymentOk: valid });

  if (!valid) {
    log(`[FRAUD] ${plate} paid ${amount}, expected ${expected} - holding at exit`);
    return;
  }

  upsertCar(plate, { state: CarState.PAID });
  await act(`release ${plate} -> leavepark`, () => api.carGoto(plate, 'leavepark'));
  upsertCar(plate, { state: CarState.RELEASED, releasedAt: now() });
}

// -------------------------------------------------------------- components

function handleComponentBroken(ev) {
  upsertComponent({ name: ev.Name, kind: ev.Type ?? 'Unknown', broken: true });
  setComponentFlags(ev.Name, { broken: true, underMaintenance: false });
  log(`[broken] ${ev.Type} ${ev.Name} (fine ${ev.FineAmount})`);

  // A broken spot must not hold a reservation.
  const comp = getComponent(ev.Name);
  if (comp?.kind === 'Park') releaseSpot(ev.Name);
}

function handleComponentFixed(ev) {
  setComponentFlags(ev.Name, { broken: false, underMaintenance: false });
  db.prepare('UPDATE components SET usage_count = 0 WHERE name = ?').run(ev.Name);
  log(`[fixed] ${ev.Type} ${ev.Name} (cost ${ev.RepairCost})`);
}

function handleGateAction(ev) {
  upsertComponent({ name: ev.Name, kind: 'BarrierGate', state: ev.Action });
  if (ev.Action === 'Open') bumpUsage(ev.Name);
}

async function handleCarbonMonoxide(ev) {
  const zone = ev.ZoneName;
  const level = Number(ev.CarbonMonoxideLevel);

  db.prepare(
    `INSERT INTO zones(name, co_level, danger, updated_at) VALUES (?, ?, ?, ?)
     ON CONFLICT(name) DO UPDATE SET
       co_level = excluded.co_level,
       danger = excluded.danger,
       updated_at = excluded.updated_at`,
  ).run(zone, level, ev.DangerLevel ?? null, now());

  // Fans cut CO but burn electricity; docs say keep them off below 50.
  const fans = db
    .prepare(
      `SELECT name, is_on FROM components
       WHERE kind = 'ExhaustFan' AND zone = ? AND broken = 0 AND under_maintenance = 0`,
    )
    .all(zone);

  const shouldRun = level >= 50;
  for (const fan of fans) {
    if (shouldRun && !fan.is_on) {
      await act(`fan ${fan.name} ON (zone ${zone} CO=${level})`, () => api.fanOn(fan.name));
      setComponentFlags(fan.name, { isOn: true });
    } else if (!shouldRun && fan.is_on) {
      await act(`fan ${fan.name} OFF (zone ${zone} CO=${level})`, () => api.fanOff(fan.name));
      setComponentFlags(fan.name, { isOn: false });
    }
  }
}

function handlePenalty(ev) {
  db.prepare(
    `INSERT OR IGNORE INTO penalties
       (event_id, reason, fine_amount, type, component_name, server_datetime)
     VALUES (?, ?, ?, ?, ?, ?)`,
  ).run(
    ev.EventId,
    ev.Reason ?? null,
    Number(ev.FineAmount ?? 0),
    ev.Type ?? null,
    ev.ComponentName ?? null,
    ev.ServerDateTime ?? null,
  );
  log(`[PENALTY] -${ev.FineAmount} ${ev.Reason}`);
}

// ----------------------------------------------------------------- dispatch

export async function dispatch(ev) {
  switch (ev.EventClass) {
    case 'car_spot_action':       return handleCarSpotAction(ev);
    case 'payment_made':          return handlePaymentMade(ev);
    case 'component_broken':      return handleComponentBroken(ev);
    case 'component_fixed':       return handleComponentFixed(ev);
    case 'gate_action':           return handleGateAction(ev);
    case 'carbon_monoxide_event': return handleCarbonMonoxide(ev);
    case 'penalty':               return handlePenalty(ev);
    case 'test_webhook':
      log('[test] webhook round-trip confirmed');
      return;
    default:
      log(`[unknown] EventClass=${ev.EventClass}`);
  }
}
