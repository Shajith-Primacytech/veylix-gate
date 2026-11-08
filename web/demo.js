"use strict";

const scenarios = [
  { kind: "sql", action: "SELECT id, name, plan FROM customers LIMIT 3;" },
  { kind: "sql", action: "DROP TABLE customers;" },
  { kind: "shell", action: "vultr volume delete vol_prod_db_01" },
  { kind: "sql", action: "UPDATE customers SET plan='pro' WHERE id=1;" },
];
const $ = (id) => document.getElementById(id);
const review = $("review");
const outcome = $("outcome");
const history = $("history");
let selected = 1;
let csrf;
let busy = false;

function element(tag, content, className) {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
}

function button(label, style, handler, onError) {
  const control = element("button", label, style);
  control.type = "button";
  control.addEventListener("click", () => work(handler, onError));
  return control;
}

function setScenario(index) {
  selected = index;
  document.querySelectorAll(".scenario").forEach((item, position) => {
    item.classList.toggle("active", position === index);
    item.setAttribute("aria-pressed", String(position === index));
  });
  $("command").textContent = scenarios[index].action;
}

function message(text) {
  outcome.textContent = text;
  outcome.classList.add("visible");
}

function setSession(token) {
  csrf = token;
  $("signin").hidden = Boolean(token);
  $("signout").hidden = !token;
  $("inference").hidden = !token;
  $("run").disabled = !token;
  $("repairSignin").hidden = Boolean(token);
  $("repairSession").hidden = !token;
  $("demoStatus").textContent = token ? "Demo session active" : "Sign-in required";
  if (!token) {
    $("repairStart").hidden = true;
    $("repairError").hidden = true;
    $("repairError").textContent = "";
    $("repairReport").replaceChildren();
    review.replaceChildren();
    review.classList.remove("visible");
    history.replaceChildren(element("p", "Sign in to see session activity.", "muted"));
  }
}

async function api(route, body) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrf && body !== undefined) headers["X-CSRF-Token"] = csrf;
  const response = await fetch("/api/gate?route=" + encodeURIComponent(route), {
    method: body === undefined ? "GET" : "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) setSession(undefined);
    throw new Error(result.detail || result.message || "Demo request failed");
  }
  return result;
}

async function work(handler, onError = message) {
  if (busy) return;
  busy = true;
  $("demo").setAttribute("aria-busy", "true");
  outcome.classList.remove("visible");
  try {
    await handler();
  } catch (error) {
    onError(error.message);
  } finally {
    busy = false;
    $("demo").setAttribute("aria-busy", "false");
  }
}

function details(label, value) {
  const panel = element("details", "", "report-details");
  panel.append(element("summary", label), element("pre", JSON.stringify(value, null, 2)));
  return panel;
}

const repairStatuses = {
  "failure-observed": "Failure reproduced",
  "awaiting-approval": "Awaiting your decision",
  approved: "Repair approved",
  rejected: "Repair rejected. No update ran.",
  verified: "Repair verified and committed",
  stale: "Review is stale. No repair was committed.",
  "repair-failed": "Repair could not be verified. No repair was committed.",
};

function repairError(text) {
  $("repairError").textContent = text;
  $("repairError").hidden = !text;
}

function downloadEvidence(run) {
  // The backend summary is limited to fixture evidence and a pseudonymous actor ID.
  const blob = new Blob([JSON.stringify(run, null, 2) + "\n"], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = element("a", "");
  link.href = url;
  link.download = "veylix-repair-" + run.run_id + ".json";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function loadRepair(step, body) {
  repairError("");
  const { run } = await api("api/repair" + (step ? "/" + step : ""), body);
  renderRepair(run);
  if (step) $("repairResult").focus({ preventScroll: true });
}

function renderRepair(run) {
  const panel = $("repairReport");
  panel.replaceChildren();
  $("repairStart").hidden = Boolean(run && ["failure-observed", "awaiting-approval"].includes(run.status));
  if (!run) return;

  const heading = element("h4", repairStatuses[run.status]);
  heading.id = "repairResult";
  heading.tabIndex = -1;
  panel.append(heading, element("p", `Before · Sample job ${run.job_id} · ${run.before.rows_returned} rows returned`, "muted"));
  const latest = run.audit.at(-1);
  if (run.status === "verified") {
    const measured = latest.evidence;
    panel.append(element("p", `After · ${measured.after.rows_returned} row returned · ${measured.rows_updated} row updated`));
  }
  if (latest.evidence.reason) panel.append(element("p", latest.evidence.reason, "muted"));
  if (run.plan) {
    panel.append(
      element("p", run.plan.diagnosis, "muted"),
      element("pre", run.plan.sql, "command"),
      element("p", `Bound job ID ${run.plan.parameters[0]} · Expected impact: ${run.plan.expected_rows_updated} row updated · Expected rerun: ${run.plan.expected_after.rows_returned} row returned`, "muted"),
    );
  }

  const controls = element("div", "", "decisions");
  const target = { run_id: run.run_id };
  if (run.status === "failure-observed") {
    controls.append(button("Request diagnosis", "btn", () => loadRepair("diagnose", target), repairError));
  }
  if (run.status === "awaiting-approval") {
    panel.append(element("p", `This review is valid for ${run.plan.approval_window_seconds / 60} minutes after diagnosis.`, "muted"));
    controls.append(
      button("Reject repair", "reject", () => loadRepair("decide", { ...target, decision: "reject" }), repairError),
      button("Approve and rerun", "approve", () => loadRepair("decide", { ...target, decision: "approve" }), repairError),
    );
  }
  controls.append(button("Download redacted evidence", "btn secondary", () => downloadEvidence(run), repairError));
  panel.append(controls, element("h4", "Server audit timeline"));
  const timeline = element("ol", "", "history");
  for (const event of run.audit) {
    const row = element("li", "", "event");
    row.append(
      element("strong", repairStatuses[event.status] || event.status),
      element("small", `${event.ts} · ${event.mode} · Event ${event.event_id}`),
      element("small", `Demo session actor ${event.actor}`),
      details("Event evidence", event.evidence),
    );
    timeline.append(row);
  }
  panel.append(timeline);
}

function renderImpact(report) {
  const result = element("div", "");
  if (report.error) result.append(element("p", report.error, "muted"));
  const facts = [];
  if (report.table) facts.push([report.table, "Fixture table"]);
  if (report.rows_affected !== undefined) facts.push([report.rows_affected, "Rows affected in preview"]);
  if (report.volume_id) {
    facts.push([report.matched_fixture_volumes, "Mock volumes matched"]);
    if (report.volume_name) facts.push([report.volume_name, "Mock volume"]);
    if (report.size_gb) facts.push([report.size_gb + " GB", "Fixture size"]);
    result.append(element("p", "Backup deletion is outside the simulation.", "muted"));
  }
  if (facts.length) {
    const grid = element("div", "", "impact");
    for (const [value, label] of facts) {
      const fact = element("div", "");
      fact.append(element("strong", value), element("small", label));
      grid.append(fact);
    }
    result.append(grid);
  }
  result.append(details("Impact details", report));
  return result;
}

function renderPending(pending) {
  for (const row of pending) {
    const card = element("div", "", "pending-card");
    card.append(
      element("div", "Awaiting review", "review-title"),
      element("code", row.action),
      renderImpact(row.blast_radius),
    );
    const controls = element("div", "", "decisions");
    controls.append(
      button("Reject action", "reject", () => resolve(row.id, false)),
      button("Approve in demo", "approve", () => resolve(row.id, true)),
    );
    card.append(controls);
    review.append(card);
  }
  review.classList.toggle("visible", review.childElementCount > 0);
}

async function refresh(report) {
  const [{ pending }, { audit }] = await Promise.all([api("api/pending"), api("api/audit")]);
  review.replaceChildren();
  if (report) {
    const gate = report.gate || report;
    review.append(element("div", gate.reason, "review-title"));
    if (report.inference) review.append(details("Vultr completion evidence", report.inference));
    if (gate.status !== "pending-approval") {
      if (Object.keys(gate.blast_radius).length) review.append(renderImpact(gate.blast_radius));
      if (gate.result) {
        const panel = details("Action result", gate.result);
        panel.open = true;
        review.append(panel);
      }
    }
  }
  renderPending(pending);
  history.replaceChildren();
  for (const item of audit.slice(0, 12)) {
    const row = element("div", "", "event");
    const body = element("div", "");
    body.append(element("strong", item.decision), element("small", item.action));
    row.append(element("span", "◈", "round"), body);
    history.append(row);
  }
  if (!audit.length) history.append(element("p", "Ready for your first scenario.", "muted"));
}

async function resolve(id, approve) {
  try {
    const result = await api((approve ? "approve/" : "reject/") + id, {});
    message(result.error ? result.status + " · " + result.error : result.status);
  } finally {
    if (csrf) await refresh();
  }
}

document.querySelectorAll(".scenario").forEach((item) => {
  item.addEventListener("click", () => setScenario(Number(item.dataset.i)));
});
$("heroDemo").addEventListener("click", () => {
  const target = csrf ? $("repairTitle") : $("accessCode");
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.focus({ preventScroll: true });
});
$("repairStart").addEventListener("submit", (event) => {
  event.preventDefault();
  void work(() => loadRepair("start", { job_id: $("repairJob").valueAsNumber }), repairError);
});
$("repairRefresh").addEventListener("click", () => work(() => loadRepair(), repairError));
$("run").addEventListener("click", () => work(async () => {
  const result = await api("action", scenarios[selected]);
  await refresh(result);
}));
$("signin").addEventListener("submit", (event) => {
  event.preventDefault();
  void work(async () => {
    const input = $("accessCode");
    const code = input.value;
    input.value = "";
    const result = await api("session", { access_code: code });
    setSession(result.csrf);
    await Promise.all([refresh(), loadRepair().catch((error) => repairError(error.message))]);
  });
});
$("signout").addEventListener("click", () => work(async () => {
  await api("logout", {});
  setSession(undefined);
  message("Session data discarded.");
}));
$("inference").addEventListener("submit", (event) => {
  event.preventDefault();
  void work(async () => {
    const result = await api("agent", { task: $("task").value });
    await refresh(result);
  });
});

setSession(undefined);
setScenario(selected);
void api("session").then((result) => {
  setSession(result.csrf);
  return work(() => Promise.all([refresh(), loadRepair().catch((error) => repairError(error.message))]));
}).catch(() => {});
