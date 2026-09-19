import { h, s, setText } from "../core/dom.js";
import { money, spotState } from "../core/format.js";

// Headline numbers (Grafana-style: label, value, context, trend).
const SPARK_SAMPLES = 120;  // ~2 minutes at one snapshot per second

export function createKpis(container) {
  const cards = {
    free: card("Free bays"),
    onSite: card("Vehicles on site"),
    money: card("Revenue"),
    penalties: card("Penalties"),
  };
  const spark = s("svg", { class: "spark", viewBox: `0 0 ${SPARK_SAMPLES} 40`, preserveAspectRatio: "none", "aria-hidden": "true" });
  const sparkLine = s("polyline");
  const sparkArea = s("polygon");
  spark.append(sparkArea, sparkLine);
  cards.free.el.append(spark);
  container.append(...Object.values(cards).map((c) => c.el));
  const history = [];

  function update(snapshot, stats, isAdmin) {
    const park = (snapshot.spots || []).filter((sp) => sp.purpose === "Park");
    const free = park.filter((sp) => spotState(sp) === "free").length;
    setText(cards.free.value, free);
    setText(cards.free.small, ` / ${park.length}`);
    setText(cards.free.sub, park.length ? `${Math.round((1 - free / park.length) * 100)}% occupied` : "No bays synced yet");
    cards.free.el.classList.toggle("is-bad", park.length > 0 && free === 0);

    history.push(park.length ? free / park.length : 0);
    if (history.length > SPARK_SAMPLES) history.shift();
    const points = history.map((v, i) => `${i + SPARK_SAMPLES - history.length},${38 - v * 34}`).join(" ");
    sparkLine.setAttribute("points", points);
    sparkArea.setAttribute("points", `${SPARK_SAMPLES - history.length},40 ${points} ${SPARK_SAMPLES},40`);

    const sessions = snapshot.sessions || [];
    const drivingIn = sessions.filter((x) => x.phase === "ASSIGNED" || x.phase === "ARRIVED").length;
    const atExit = sessions.filter((x) => ["AT_EXIT", "CHARGED", "EXIT_REQUESTED"].includes(x.phase)).length;
    setText(cards.onSite.value, sessions.length);
    setText(cards.onSite.sub, `${drivingIn} arriving · ${atExit} at exit`);

    cards.penalties.el.hidden = !isAdmin;
    if (!stats) return;
    if (isAdmin) {
      setText(cards.money.label, "Revenue");
      setText(cards.money.value, money(stats.revenue));
      setText(cards.money.sub, `Net after fines ${money(stats.net)}`);
    } else {
      setText(cards.money.label, "Completed stays");
      setText(cards.money.value, stats.completed_sessions);
      setText(cards.money.sub, `Average ${Number(stats.avg_minutes || 0).toFixed(1)} min`);
    }
    setText(cards.penalties.value, stats.penalty_count);
    setText(cards.penalties.sub, stats.penalty_count ? `−${money(stats.total_fines)} in fines` : "None so far");
    cards.penalties.el.classList.toggle("is-bad", stats.penalty_count > 0);
    cards.penalties.el.classList.toggle("is-good", stats.penalty_count === 0);
  }

  return { update };
}

function card(labelText) {
  const label = h("div", { class: "kpi-label", text: labelText });
  const value = h("span", { text: "—" });
  const small = h("small");
  const sub = h("div", { class: "kpi-sub", text: "" });
  const el = h("section", { class: "kpi" }, label, h("div", { class: "kpi-value" }, value, small), sub);
  return { el, label, value, small, sub };
}
