import { api, runAction } from "./api.js";
import { confirmAction } from "./toast.js";

// Red dustbin on History / Payments / Penalties. The button is rendered with
// data-cap="admin:reset", so the shell hides it from everyone but admins; the
// server enforces the same capability on DELETE /api/admin/data/{section}.
export function wireClear(buttonId, section, label, reload) {
  const button = document.getElementById(buttonId);
  if (!button) return;
  button.addEventListener("click", async () => {
    const ok = await confirmAction({
      title: `Delete all ${label} records?`,
      message: `Every ${label} record is permanently removed. Cars still on site are not affected. This cannot be undone.`,
      confirmLabel: "Delete", danger: true,
    });
    if (!ok) return;
    const result = await runAction(button, () => api(`/api/admin/data/${section}`, { method: "DELETE" }),
      (r) => `Deleted ${r.removed} ${label} record${r.removed === 1 ? "" : "s"}`);
    if (result) await reload();
  });
}
