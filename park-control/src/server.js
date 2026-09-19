import express from 'express';
import { config } from './config.js';
import { db, insertEvent, markProcessed, trackSequence, getMeta } from './db.js';
import { verifySignature, signatureReport, signatureAttempts } from './signature.js';
import { dispatch } from './handlers.js';
import { bootstrapFromSimulator, freeSpotCount } from './state.js';

const app = express();
app.use(express.json({ limit: '1mb' }));

const log = (...a) => console.log(new Date().toISOString(), ...a);

/*
 * Webhook intake.
 *
 * The simulator is a real-time game; if we block it while we think, we fall
 * behind and cars get neglected. So: persist synchronously (a single SQLite
 * write, sub-millisecond), acknowledge immediately, then process off the
 * response path.
 */
app.post('/webhook', (req, res) => {
  const ev = req.body;

  if (!ev || typeof ev !== 'object' || !ev.EventClass) {
    return res.status(400).json({ error: 'not a simulator event' });
  }

  const sig = verifySignature(ev);

  if (sig.enforced && sig.ok === false) {
    log(`[sig:REJECT] ${ev.EventClass} ${ev.EventId}`);
    return res.status(401).json({ error: 'bad signature' });
  }

  // EventId is the idempotency key -- the docs tell us to store and check it
  // before processing so a redelivery is not handled twice.
  const isNew = insertEvent({
    eventId: ev.EventId,
    sequenceId: typeof ev.SequenceId === 'number' ? ev.SequenceId : Number(ev.SequenceId),
    eventClass: ev.EventClass,
    serverDateTime: ev.ServerDateTime ?? null,
    realDateTime: ev.RealDateTime ?? null,
    signature: ev.Signature ?? null,
    signatureOk: sig.ok,
    payload: JSON.stringify(ev),
  });

  res.status(200).json({ ok: true, duplicate: !isNew });

  if (!isNew) {
    log(`[dup] ${ev.EventClass} ${ev.EventId} already seen`);
    return;
  }

  trackSequence(Number(ev.SequenceId));

  // Fire and forget, but never lose the error.
  Promise.resolve()
    .then(() => dispatch(ev))
    .then(() => markProcessed(ev.EventId))
    .catch((err) => {
      log(`[handler:ERROR] ${ev.EventClass} ${ev.EventId}: ${err.stack ?? err.message}`);
      markProcessed(ev.EventId, String(err.message));
    });
});

// Some setups default to GET for reachability checks.
app.get('/webhook', (_req, res) => res.json({ ok: true, listening: true }));

app.get('/health', (_req, res) => {
  const counts = db
    .prepare(
      `SELECT
         (SELECT COUNT(*) FROM events)        AS events,
         (SELECT COUNT(*) FROM cars)          AS live_cars,
         (SELECT COUNT(*) FROM sessions)      AS completed,
         (SELECT COUNT(*) FROM penalties)     AS penalties,
         (SELECT COALESCE(SUM(fine_amount),0) FROM penalties) AS fines,
         (SELECT COUNT(*) FROM sequence_gaps) AS gaps,
         (SELECT COUNT(*) FROM payments WHERE valid = 0) AS bad_payments`,
    )
    .get();

  res.json({
    ok: true,
    autopilot: config.autopilot,
    signatureMode: config.signature.mode,
    signaturePinned: config.signature.recipe,
    lastSequenceId: Number(getMeta('last_sequence_id', '0')),
    freeSpots: freeSpotCount(),
    ...counts,
  });
});

app.get('/signature-report', (_req, res) =>
  res.json({ attempts: signatureAttempts(), candidates: signatureReport() }),
);

app.get('/events', (req, res) => {
  const limit = Math.min(Number(req.query.limit ?? 50), 500);
  const cls = req.query.class;
  const rows = cls
    ? db
        .prepare(
          'SELECT * FROM events WHERE event_class = ? ORDER BY sequence_id DESC LIMIT ?',
        )
        .all(cls, limit)
    : db.prepare('SELECT * FROM events ORDER BY sequence_id DESC LIMIT ?').all(limit);
  res.json(rows);
});

app.get('/cars', (_req, res) =>
  res.json(db.prepare('SELECT * FROM cars ORDER BY updated_at DESC').all()),
);

app.get('/components', (_req, res) =>
  res.json(db.prepare('SELECT * FROM components ORDER BY kind, name').all()),
);

app.get('/penalties', (_req, res) =>
  res.json(db.prepare('SELECT * FROM penalties ORDER BY server_datetime DESC LIMIT 200').all()),
);

/** Manual re-sync, for use after a crash. Never call this on a timer. */
app.post('/resync', async (_req, res) => {
  try {
    const summary = await bootstrapFromSimulator();
    res.json({ ok: true, ...summary });
  } catch (err) {
    res.status(502).json({ ok: false, error: err.message });
  }
});

app.listen(config.port, '0.0.0.0', async () => {
  log(`listener up on http://0.0.0.0:${config.port}/webhook`);
  log(`autopilot=${config.autopilot}  signature=${config.signature.mode}`);
  log(`point settings.json -> "WebhookUrl": "http://<your-ip>:${config.port}/webhook"`);

  try {
    const summary = await bootstrapFromSimulator();
    log(`bootstrap sync: ${JSON.stringify(summary)}`);
  } catch (err) {
    log(`bootstrap sync failed (is the simulator running?): ${err.message}`);
    log('the listener still works; POST /resync once the simulator is up');
  }
});
