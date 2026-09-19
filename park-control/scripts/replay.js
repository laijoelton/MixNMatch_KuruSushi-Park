/*
 * Replay a synthetic car lifecycle against the running listener, so you can
 * exercise the state machine before the simulator is wired up.
 *
 *   node scripts/replay.js
 *
 * Events use the exact shapes from the webhook documentation.
 */
const BASE = process.env.TARGET ?? 'http://127.0.0.1:4000/webhook';
const PLATE = process.env.PLATE ?? 'WCT 759';

let seq = Number(process.env.START_SEQ ?? 9000);
const uuid = () => crypto.randomUUID();
const stamp = () => new Date().toISOString().slice(0, 19).replace('T', ' ');

async function send(event) {
  const body = { ...event, EventId: uuid(), SequenceId: seq++, ServerDateTime: stamp() };
  const res = await fetch(BASE, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  console.log(`${String(body.SequenceId).padEnd(6)} ${body.EventClass.padEnd(22)} -> ${res.status} ${text}`);
  return res;
}

const carEvent = (spotName, spotType, direction, extra = {}) => ({
  EventClass: 'car_spot_action',
  CarPlateNumber: PLATE,
  SpotName: spotName,
  SpotType: spotType,
  CarType: 'Normal',
  Direction: direction,
  PlannedParkingDurationInMinutes: '0',
  ...extra,
});

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  console.log(`replaying a full car lifecycle for ${PLATE} against ${BASE}\n`);

  await send({ EventClass: 'test_webhook' });

  await send(carEvent('ENTRY1', 'EntrySpot', 'CarIn'));
  await wait(300);
  await send(carEvent('ENTRY1', 'EntrySpot', 'CarOut'));
  await wait(300);

  await send(carEvent('S3', 'Park', 'CarIn', { PlannedParkingDurationInMinutes: '2' }));
  console.log('\n... car is parked, waiting 3s to accrue billable time ...\n');
  await wait(3000);

  await send(carEvent('S3', 'Park', 'CarOut', { PlannedParkingDurationInMinutes: '2' }));
  await wait(300);
  await send(carEvent('EXIT_EXIT', 'ExitSpot', 'CarIn'));
  await wait(300);

  // Honest payment. Flip to a wrong number to watch fraud detection fire.
  await send({
    EventClass: 'payment_made',
    CarPlateNumber: PLATE,
    Amount: process.env.AMOUNT ?? '1.00',
    Reason: 'Car Payment',
  });
  await wait(300);

  await send(carEvent('EXIT_EXIT', 'ExitSpot', 'CarOut'));

  console.log('\n--- extras ---');
  await send({
    EventClass: 'component_broken',
    Type: 'BarrierGate',
    Name: 'gateA',
    FineAmount: '10.00',
  });
  await send({
    EventClass: 'penalty',
    Reason: 'BarrierGate: Cannot Operate if it is Broken or under Maintenance.',
    FineAmount: '10',
    Type: 'BarrierGate',
    ComponentName: 'gateA',
  });
  await send({
    EventClass: 'carbon_monoxide_event',
    ZoneName: 'ZONE1',
    CarbonMonoxideLevel: 63.564693,
    DangerLevel: 'Mid',
  });

  // Duplicate delivery: must be rejected as already seen.
  console.log('\n--- duplicate EventId check ---');
  const dupe = {
    EventClass: 'gate_action',
    Name: 'gateA',
    Action: 'Open',
    EventId: 'fixed-id-for-dupe-test',
    SequenceId: seq++,
    ServerDateTime: stamp(),
  };
  for (let i = 0; i < 2; i++) {
    const res = await fetch(BASE, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(dupe),
    });
    console.log(`  attempt ${i + 1} -> ${await res.text()}`);
  }

  // Sequence gap: should be recorded, not silently swallowed.
  console.log('\n--- sequence gap check ---');
  seq += 25;
  await send({ EventClass: 'gate_action', Name: 'gateA', Action: 'Closed' });

  console.log('\nnow check:  curl http://127.0.0.1:4000/health');
}

main().catch((err) => {
  console.error('replay failed:', err.message);
  console.error('is the listener running?  npm start');
  process.exit(1);
});
