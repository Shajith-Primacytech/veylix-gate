"use strict";

const byId = (id) => document.getElementById(id);
const [login, workspace, start, statusText, details, diagnose, approve, reject, audit, logout, notice] =
  ["login", "workspace", "start", "status", "details", "diagnose", "approve", "reject", "export", "logout", "notice"].map(byId);
let csrf;
let run;
let busy = false;
const terminal = ["verified", "rejected", "stale", "repair-failed"];
const labels = {
  starting: "Starting the Docker worker", "failure-observed": "Sample failed: 0 rows returned",
  diagnosing: "Waiting for Vultr inference", "awaiting-approval": "Proposal ready for review",
  approved: "Approved; running the isolated task", verified: "Repair committed and verified",
  rejected: "Proposal rejected; no repair worker started", stale: "Approval expired",
  "repair-failed": "Sandbox or repair verification failed",
};

async function request(path, data) {
  const res = await fetch(path, data === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf || ""},
    body: JSON.stringify(data),
  });
  const result = await res.json();
  if (res.status === 401 && path !== "/session") {
    csrf = run = undefined;
    render();
  }
  if (!res.ok) throw new Error(result.detail || "Request failed");
  return result;
}

function render() {
  login.hidden = Boolean(csrf);
  workspace.hidden = !csrf;
  const status = run?.status;
  statusText.textContent = labels[status] || "No active run";
  start.querySelector("button").disabled = busy || Boolean(status && !terminal.includes(status));
  diagnose.hidden = status !== "failure-observed";
  approve.hidden = reject.hidden = status !== "awaiting-approval";
  audit.hidden = !run;
  for (const button of [login.querySelector("button"), diagnose, approve, reject, audit, logout]) button.disabled = busy;
  const inference = run?.audit.find((entry) => entry.status === "awaiting-approval")?.evidence.inference;
  const outcome = run?.audit.at(-1)?.evidence;
  details.textContent = run ? JSON.stringify({job: run.job_id, before: run.before, inference,
    proposal: run.plan, outcome: status === "verified" ? {after: outcome.after, rows_updated: outcome.rows_updated,
      container_id: outcome.sandbox.find((item) => item.event === "completed")?.container_id} : undefined}, null, 2) : "";
}

async function act(fn) {
  if (busy) return;
  busy = true;
  notice.textContent = "";
  render();
  try { await fn(); } catch (e) { notice.textContent = e.message; }
  finally { busy = false; render(); }
}

login.addEventListener("submit", (ev) => {
  ev.preventDefault();
  act(async () => {
    const code = login.elements.access_code;
    try { ({csrf} = await request("/session", {access_code: code.value})); }
    finally { code.value = ""; }
    ({run} = await request("/api/repair"));
  });
});
start.addEventListener("submit", (ev) => {
  ev.preventDefault();
  act(async () => { ({run} = await request("/api/repair/start", {job_id: Number(start.elements.job_id.value)})); });
});
diagnose.onclick = () => act(async () => { ({run} = await request("/api/repair/diagnose", {run_id: run.run_id})); });
for (const [button, decision] of [[approve, "approve"], [reject, "reject"]]) {
  button.onclick = () => act(async () => { ({run} = await request("/api/repair/decide", {run_id: run.run_id, decision})); });
}
logout.onclick = () => act(async () => {
  await request("/logout", {});
  csrf = run = undefined;
});
audit.onclick = () => act(async () => {
  const snapshot = await request("/api/audit");
  const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], {type: "application/json"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = "repairlab-" + run.run_id + ".json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
act(async () => {
  try { ({csrf} = await request("/session")); }
  catch (e) { if (e.message !== "Start an authenticated demo session") throw e; }
  if (csrf) ({run} = await request("/api/repair"));
});
