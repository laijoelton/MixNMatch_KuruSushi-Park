// One WebSocket to /ws/live for the whole page. The server pushes a full
// snapshot every second; widgets subscribe and render from it.
//
// Degrades instead of breaking: on disconnect the last snapshot stays on
// screen (dimmed, with its age) and the socket retries with backoff.

const subscribers = new Set();
const connectionListeners = new Set();
let socket = null;
let attempts = 0;
let lastSnapshot = null;
let lastUpdateAt = 0;
let state = "connecting";

export function subscribe(fn) {
  subscribers.add(fn);
  if (lastSnapshot) safeCall(fn, lastSnapshot);
  return () => subscribers.delete(fn);
}

export function onConnection(fn) {
  connectionListeners.add(fn);
  fn(state, lastUpdateAt);
  return () => connectionListeners.delete(fn);
}

export function snapshot() {
  return lastSnapshot;
}

export function lastUpdate() {
  return lastUpdateAt;
}

function setState(next) {
  state = next;
  for (const fn of connectionListeners) safeCall(fn, state, lastUpdateAt);
}

function safeCall(fn, ...args) {
  try {
    fn(...args);
  } catch (error) {
    console.error("live subscriber failed", error);  // one broken widget must not stop the rest
  }
}

export function connect() {
  if (socket) return;
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${protocol}://${location.host}/ws/live`);

  socket.addEventListener("open", () => {
    attempts = 0;
    setState("live");
  });

  socket.addEventListener("message", (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch {
      return;
    }
    if (data.type === "alert") {
      window.dispatchEvent(new CustomEvent("park-alert", { detail: data }));
      return;
    }
    lastSnapshot = data;
    lastUpdateAt = Date.now();
    for (const fn of subscribers) safeCall(fn, data);
  });

  socket.addEventListener("close", (event) => {
    socket = null;
    if (event.code === 4401) {
      location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
      return;
    }
    setState("reconnecting");
    attempts += 1;
    setTimeout(connect, Math.min(10000, 1000 * 2 ** Math.min(attempts - 1, 4)));
  });

  socket.addEventListener("error", () => socket && socket.close());
}
