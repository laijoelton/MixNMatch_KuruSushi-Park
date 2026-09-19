import { config } from './config.js';

let token = null;
let loginInFlight = null;

class SimError extends Error {
  constructor(status, path, body) {
    super(`${status} on ${path}: ${body}`);
    this.name = 'SimError';
    this.status = status;
    this.path = path;
  }
}

async function login() {
  const res = await fetch(`${config.sim.baseUrl}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ email: config.sim.email, password: config.sim.password }),
  });
  if (!res.ok) throw new SimError(res.status, '/auth/login', await res.text());
  const data = await res.json();
  if (!data?.token) throw new Error('login succeeded but no token in response');
  token = data.token;
  return token;
}

/** Serialise concurrent logins so a burst of 401s triggers exactly one refresh. */
function ensureToken() {
  if (token) return Promise.resolve(token);
  loginInFlight ??= login().finally(() => {
    loginInFlight = null;
  });
  return loginInFlight;
}

async function request(method, path, { retryOn401 = true } = {}) {
  await ensureToken();
  const res = await fetch(`${config.sim.baseUrl}${path}`, {
    method,
    headers: { authorization: `Bearer ${token}` },
  });

  if (res.status === 401 && retryOn401) {
    token = null;
    return request(method, path, { retryOn401: false });
  }
  if (!res.ok) throw new SimError(res.status, path, await res.text());

  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

const get = (p) => request('GET', p);
const post = (p) => request('POST', p);

/**
 * Plates arrive as "WCT 759"; the API accepts them with or without the space.
 * We strip it so one canonical form is used everywhere, including as our
 * database key.
 */
export const normalisePlate = (plate) => String(plate ?? '').replace(/\s+/g, '').toUpperCase();

export const api = {
  login,
  get token() {
    return token;
  },

  // --- Discovery. Documented as expensive: bootstrap and crash-recovery only.
  listParkingSpots: () => get('/api/v1/list-parking-spots'),
  listBarriers: () => get('/api/v1/list-barriers'),
  listLights: () => get('/api/v1/list-lights'),
  listExhaustFans: () => get('/api/v1/list-exhaust-fans'),
  listAlarms: () => get('/api/v1/list-alarms'),
  listZones: () => get('/api/v1/list-zones'),
  test: () => get('/api/v1/test'),

  // --- Control
  openGate: (name) => post(`/api/v1/barrier-gates/${encodeURIComponent(name)}/open`),
  closeGate: (name) => post(`/api/v1/barrier-gates/${encodeURIComponent(name)}/close`),
  repairGate: (name) => post(`/api/v1/barrier-gates/${encodeURIComponent(name)}/repair`),

  lightOn: (name) => post(`/api/v1/lights/${encodeURIComponent(name)}/on`),
  lightOff: (name) => post(`/api/v1/lights/${encodeURIComponent(name)}/off`),
  lightGroupOn: (group) => post(`/api/v1/lights/group/${encodeURIComponent(group)}/on`),
  lightGroupOff: (group) => post(`/api/v1/lights/group/${encodeURIComponent(group)}/off`),

  fanOn: (name) => post(`/api/v1/exhaust-fans/${encodeURIComponent(name)}/on`),
  fanOff: (name) => post(`/api/v1/exhaust-fans/${encodeURIComponent(name)}/off`),
  repairFan: (name) => post(`/api/v1/exhaust-fans/${encodeURIComponent(name)}/repair`),

  repairSpot: (name) => post(`/api/v1/parking-spots/${encodeURIComponent(name)}/repair`),

  /** destination: a spot name, "exit" (pays on the way), or "leavepark". */
  carGoto: (plate, destination) =>
    post(
      `/api/v1/car/${encodeURIComponent(normalisePlate(plate))}/goto/${encodeURIComponent(destination)}`,
    ),

  charge: (plate, parkingCost, chargingCost = 0) =>
    post(
      `/api/v1/car/${encodeURIComponent(normalisePlate(plate))}/charge` +
        `?parkingCost=${encodeURIComponent(parkingCost)}` +
        `&chargingCost=${encodeURIComponent(chargingCost)}`,
    ),
};

export { SimError };
