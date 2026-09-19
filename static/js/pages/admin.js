import { api, runAction } from "../core/api.js";
import { clear, emptyState, h, setText } from "../core/dom.js";
import { dateTime } from "../core/format.js";
import { initShell } from "../core/shell.js";
import { confirmAction } from "../core/toast.js";

const me = await initShell();

// ------------------------------------------------------------------ accounts
async function loadUsers() {
  const users = await api("/api/admin/users");
  setText(document.getElementById("users-count"), users.length);
  const body = document.getElementById("users");
  clear(body);
  for (const user of users) {
    const self = user.username === me.username;
    const del = h("button", { class: "btn danger small", type: "button", text: "Remove", disabled: self,
      title: self ? "You cannot remove your own account" : null,
      onclick: async () => {
        const ok = await confirmAction({ title: `Remove ${user.username}?`, message: "They will be signed out immediately.", confirmLabel: "Remove", danger: true });
        if (ok && await runAction(del, () => api(`/api/admin/users/${user.id}`, { method: "DELETE" }), `${user.username} removed`)) {
          loadUsers();
          loadAudit();
        }
      } });
    body.append(h("tr", {},
      h("td", {}, h("b", { text: user.username }), self ? h("span", { class: "muted", text: " (you)" }) : null),
      h("td", {}, h("span", { class: `tag ${user.role === "admin" ? "accent" : ""}`, text: user.role })),
      h("td", { text: dateTime(user.created_at) }),
      h("td", { style: "text-align:right" }, del)));
  }
}

const userForm = document.getElementById("new-user");
userForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!userForm.reportValidity()) return;
  const body = { username: userForm.username.value.trim(), password: userForm.password.value, role: userForm.role.value };
  if (await runAction(document.getElementById("nu-btn"), () => api("/api/admin/users", { method: "POST", body }), `Account ${body.username} created`)) {
    userForm.reset();
    loadUsers();
    loadAudit();
  }
});

// ------------------------------------------------------------------ audit trail
async function loadAudit() {
  const rows = await api("/api/admin/audit?limit=200");
  const body = document.getElementById("audit");
  clear(body);
  if (!rows.length) body.append(h("tr", {}, h("td", { colspan: 5 }, emptyState("No changes yet", "Actions such as opening a gate are recorded here."))));
  for (const row of rows) {
    const ok = row.status != null && row.status < 400;
    body.append(h("tr", {},
      h("td", { text: dateTime(row.at) }),
      h("td", { text: row.username }),
      h("td", { class: "num", style: "text-align:left", text: row.method }),
      h("td", { class: "num", style: "text-align:left", text: row.path }),
      h("td", { class: "num" }, h("span", { class: `tag ${ok ? "free" : "fault"}`, text: row.status ?? "—" }))));
  }
}

// ------------------------------------------------------------------ tools
document.getElementById("sync-btn").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const ok = await confirmAction({
    title: "Resync with the simulator?",
    message: "This calls the simulator's list APIs, which carry an operating cost. Use it after a crash or a level change.",
    confirmLabel: "Resync",
  });
  if (ok && await runAction(button, () => api("/api/manual/sync", { method: "POST" }),
    (r) => `Synced ${r.spots} bays, ${r.barriers} gates, ${r.zones} zones, ${r.fans} fans`)) loadAudit();
});

const simForm = document.getElementById("sim-form");
const snapshot = await api("/api/state");
const gateSelect = document.getElementById("sim-gate");
for (const spot of (snapshot.spots || []).filter((x) => x.purpose === "EntrySpot")) gateSelect.append(h("option", { text: spot.name }));
if (!gateSelect.options.length) gateSelect.append(h("option", { value: "", text: "No entries synced" }));

simForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const out = document.getElementById("sim-out");
  const body = {
    plate: simForm.plate.value.trim(), gate: gateSelect.value, car_type: simForm.car_type.value, dry_run: simForm.dry_run.checked,
  };
  if (!body.plate || !body.gate) return;
  const result = await runAction(document.getElementById("sim-btn"), () => api("/api/manual/arrival", { method: "POST", body }));
  if (!result) return;
  out.hidden = false;
  out.textContent = result.target
    ? `${result.dispatched ? "Dispatched" : "Would send"} ${result.plate} → ${result.target}` +
      (result.ranked_candidates ? `\nRanking: ${result.ranked_candidates.map(([s, d]) => `${s} (${d})`).join(", ")}` : "")
    : `No bay available for ${result.plate}${result.reason ? ` — ${result.reason}` : ""}`;
  loadAudit();
});

await Promise.all([loadUsers(), loadAudit()]);
