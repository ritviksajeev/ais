/* AiS review UI.
 *
 * Polls the local server for the state of the run, draws it, and posts back the
 * one thing a human contributes: approve or reject. No framework, no build.
 *
 * Everything rendered here originates from an edit proposed by an agent --
 * file paths, rationales, stdout, trace arguments. It is all escaped before it
 * reaches the DOM. A review tool that could be attacked by the thing it is
 * reviewing would be a poor review tool.
 */

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const POLL_MS = 350;

/* The seven steps, and which audit stages light each one. This is the story of
   the system told once, in order -- the rail is the main way someone who has
   never seen AiS learns what it actually does. */
const STEPS = [
  { who: "Editor",   name: "Edit proposed",
    text: "An agent submits file contents. It gets no real path and no file handle.",
    stages: ["request_received"] },
  { who: "Mediator", name: "Sandbox built",
    text: "Only the files this edit needs are copied into a fresh disposable workspace.",
    stages: ["plan_built", "sandbox_materialized", "plan_rejected"] },
  { who: "Mediator", name: "Diff computed",
    text: "The proposal is compared with the real file to produce the exact patch.",
    stages: ["diff_computed"] },
  { who: "Sandbox",  name: "Code executed",
    text: "The patch runs inside the sandbox with no network and hard limits, while every file open, socket and subprocess is watched.",
    stages: ["sandbox_executed"] },
  { who: "Verifier", name: "Behaviour checked",
    text: "Rules run over what was observed, not over the text of the diff.",
    stages: ["verified"] },
  { who: "You",      name: "Human decides",
    text: "You read the diff and the evidence and make the call. The verdict is advice.",
    stages: ["decided"] },
  { who: "Mediator", name: "Committed or discarded",
    text: "Approve writes a git commit you can revert. Reject destroys the sandbox and the real file was never touched.",
    stages: ["applied", "discarded", "errored"] },
];

const STAGE_TO_STEP = {};
STEPS.forEach((step, i) => step.stages.forEach((s) => (STAGE_TO_STEP[s] = i)));

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let seq = 0;
let currentRequest = null;
let submitting = false;
let reachedStep = -1;

/* ---------- boot ---------- */

function drawSteps() {
  $("steps").innerHTML = STEPS.map((s, i) => `
    <li class="step" data-n="${i + 1}" id="step-${i}">
      <span class="who">${esc(s.who)}</span>
      <h3>${esc(s.name)}</h3>
      <p>${esc(s.text)}</p>
    </li>`).join("");
}

function markStep(index, active) {
  STEPS.forEach((_, i) => {
    const el = $(`step-${i}`);
    el.classList.toggle("done", i < index || (i === index && !active));
    el.classList.toggle("active", i === index && active);
  });
}

/* ---------- polling ---------- */

async function api(path, options = {}) {
  const joiner = path.includes("?") ? "&" : "?";
  const response = await fetch(`${path}${joiner}t=${encodeURIComponent(TOKEN)}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) throw new Error(`${response.status}`);
  return response.json();
}

async function poll() {
  try {
    const state = await api(`/api/state?since=${seq}`);
    seq = state.seq;
    apply(state);
  } catch (err) {
    const finished = !$("results").hidden;
    $("status-line").textContent = finished ? "AiS closed" : "Lost contact with the AiS process";
    $("status-sub").textContent = finished
      ? "The run is finished and the server has shut down. This page is now a static record."
      : "The run has ended, or the terminal was closed.";
    $("spinner").classList.add("still");
    return; // stop polling: nothing is coming back
  }
  setTimeout(poll, POLL_MS);
}

function apply(state) {
  // header
  const run = state.run || {};
  const pill = $("pill-backend");
  if (run.backend) {
    pill.textContent = run.backend;
    pill.title = run.backend; // truncated in the bar; readable on hover
    pill.className = "pill " + (run.isolated ? "good" : "warn");
  }
  if (run.project) {
    $("fact-project").textContent = run.project;
    $("fact-project").title = run.project;
  }

  // status
  $("status-line").textContent = state.message || state.status;
  $("spinner").classList.toggle("still", ["finished", "error", "aborted", "awaiting_decision"].includes(state.status));

  // events
  if (state.events.length) {
    appendFeed(state.events);
    const last = state.events[state.events.length - 1];
    const index = STAGE_TO_STEP[last.stage];
    if (index !== undefined) {
      if (last.stage === "request_received") reachedStep = 0;
      else reachedStep = Math.max(reachedStep, index);
      markStep(reachedStep, state.status !== "finished");
    }
  }
  $("chain-count").textContent = seq;

  // pending review
  if (state.pending && state.pending.request_id !== currentRequest) {
    currentRequest = state.pending.request_id;
    submitting = false;
    drawReview(state.pending);
    markStep(5, true);
  } else if (!state.pending && currentRequest) {
    currentRequest = null;
    $("review").hidden = true;
    $("review").innerHTML = "";
  }

  // results
  if (state.records && state.records.length) drawResults(state.records, state.summary);
  if (state.status === "finished" || state.status === "aborted") {
    markStep(6, false);
    $("finish").hidden = false;
    $("status-sub").textContent = "The real project is at the state you approved. Nothing else was written.";
  }
}

function appendFeed(events) {
  const feed = $("feed");
  for (const event of events) {
    const li = document.createElement("li");
    li.innerHTML =
      `<span class="who" title="${esc(event.actor)}">${esc(event.component)}</span>` +
      `<span class="what">${esc(event.title)}<small>${esc(event.blurb)}</small></span>` +
      `<span class="rid">${esc(event.request_id || "")}</span>`;
    feed.prepend(li);
  }
  while (feed.children.length > 60) feed.lastChild.remove();
}

/* ---------- the review card ---------- */

function drawReview(p) {
  const el = $("review");
  el.hidden = false;
  el.innerHTML = [
    head(p),
    p.isolation_warning ? panel("", `<p class="note warn">${esc(p.isolation_warning)}</p>`) : "",
    panel("What the Verifier found", findings(p)),
    panel("The change", diff(p.diff)),
    panel("What happened when it ran", execution(p)),
    p.trace.length ? `<div class="panel" id="trace-panel"><h3>Observed operations (${p.trace_total})</h3>${trace(p)}</div>` : "",
    panel("Files copied into the sandbox", closure(p)),
    decideBar(p),
  ].join("");

  el.querySelector(".approve").addEventListener("click", () => decide(true));
  el.querySelector(".reject").addEventListener("click", () => decide(false));
  wireTrace(p);
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}

function wireTrace(p) {
  const box = document.getElementById("trace-all");
  if (!box) return;
  box.addEventListener("change", () => {
    const panelEl = $("trace-panel");
    panelEl.innerHTML = `<h3>Observed operations (${p.trace_total})</h3>` + trace(p, box.checked);
    wireTrace(p);
  });
}

const panel = (title, body) =>
  `<div class="panel">${title ? `<h3>${esc(title)}</h3>` : ""}${body}</div>`;

function head(p) {
  return `<div class="req-head">
    <div class="req-top">
      <div>
        <div class="req-id">${esc(p.request_id)} · ${p.index} of ${p.total} · ${esc(p.targets.join(", "))}</div>
        <h2 class="req-title">${esc(p.title)}</h2>
        <p class="req-why">${esc(p.rationale)}</p>
      </div>
      <div class="verdict ${esc(p.verdict)}">${esc(p.verdict)} · ${esc(p.headline)}</div>
    </div>
  </div>`;
}

function findings(p) {
  const all = [...p.findings, ...p.caveats.map((c) => ({ ...c, advisory: true }))];
  if (!all.length) return `<p class="clean">No anomalies. The edit did nothing unexpected when it ran.</p>`;
  return all.map((f) => `
    <div class="finding ${esc(f.severity)}">
      <div class="finding-head">
        <span class="sev">${esc(f.severity)}</span>
        <strong>${esc(f.title)}</strong>
        <span class="rule">${esc(f.rule_id)}${f.advisory ? " · about the run, not the edit" : ""}</span>
      </div>
      <p>${esc(f.detail)}</p>
      ${f.evidence.length ? `<div class="evidence">${esc(f.evidence.join("\n"))}</div>` : ""}
    </div>`).join("");
}

function diff(text) {
  if (!text.trim()) return `<p class="note">No textual change.</p>`;
  const lines = text.split("\n").map((line) => {
    let cls = "";
    if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ")) cls = "meta";
    else if (line.startsWith("@@")) cls = "hunk";
    else if (line.startsWith("+")) cls = "add";
    else if (line.startsWith("-")) cls = "del";
    return `<span class="ln ${cls}">${esc(line) || "&nbsp;"}</span>`;
  });
  return `<pre class="diff">${lines.join("")}</pre>`;
}

function execution(p) {
  const x = p.execution;
  const t = x.tests;
  const tiles = [
    tile("tests", t ? `${t.passed}/${t.total}` : "—",
         t ? (t.all_passed ? "good" : "bad") : ""),
    tile("exit code", x.exit_code === null ? "—" : x.exit_code, x.exit_code === 0 ? "good" : "bad"),
    tile("duration", `${x.duration_s}s`, x.timed_out ? "bad" : ""),
    tile("peak memory", x.max_rss_mb ? `${Math.round(x.max_rss_mb)} MB` : "—", x.oom_killed ? "bad" : ""),
    tile("patch", x.patch_applied ? "applied" : "rejected", x.patch_applied ? "good" : "bad"),
    tile("isolation", x.isolated ? "container" : "none", x.isolated ? "good" : "warn"),
  ].join("");

  const failing = t && t.failing_tests.length
    ? `<p class="note">Failing: <span class="rule">${esc(t.failing_tests.join(", "))}</span></p>` : "";
  const infra = x.infrastructure_error
    ? `<p class="note warn">${esc(x.infrastructure_error)}</p>` : "";

  return `<div class="tiles">${tiles}</div>${failing}${infra}
    ${stream("stdout", x.stdout)}${stream("stderr", x.stderr)}`;
}

const tile = (k, v, cls = "") =>
  `<div class="tile ${cls}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`;

const stream = (name, body) =>
  body && body.trim()
    ? `<details><summary>${esc(name)} (${body.length} bytes)</summary><pre class="stream">${esc(body)}</pre></details>`
    : "";

/* A sandboxed pytest run opens plenty of files that have nothing to do with the
   edit -- temp files, /dev/null, the import machinery. Defaulting to the events
   the edit itself caused is the difference between a table a person reads and a
   log they scroll past; the rest is one click away, never hidden. */
function trace(p, showAll = false) {
  const mine = p.trace.filter((e) => e.from_workspace);
  const rows = (showAll ? p.trace : mine).map((e) => {
    // The edit leaving its sandbox is the finding. The runner touching /tmp is not.
    const notable = e.escapes_workspace && e.from_workspace;
    return `
    <tr class="${notable ? "escape" : ""}">
      <td class="mono nowrap">${e.elapsed_ms}ms</td>
      <td class="mono nowrap">${esc(e.event)}</td>
      <td class="mono target">${esc(target(e))}</td>
      <td>${e.escapes_workspace ? `<span class="badge ${notable ? "hot" : ""}">left sandbox</span>` : ""}
          ${e.from_workspace ? '<span class="badge edit">from the edit</span>' : ""}</td>
      <td class="mono nowrap">${esc(e.origin || "")}</td>
    </tr>`;
  }).join("");

  const others = p.trace.length - mine.length;
  const controls = others > 0
    ? `<div class="trace-controls">
         <label><input type="checkbox" id="trace-all" ${showAll ? "checked" : ""}>
           also show the ${others} operation(s) the test runner did by itself</label>
       </div>` : "";
  const more = p.trace_total > p.trace.length
    ? `<p class="note">Showing the first ${p.trace.length} of ${p.trace_total}. The full trace is in the audit log.</p>` : "";
  const empty = !rows
    ? `<p class="clean">The edit itself performed no audited operations.</p>` : "";
  return `${controls}<table class="grid">
    <thead><tr><th>at</th><th>operation</th><th>target</th><th></th><th>caused by</th></tr></thead>
    <tbody>${rows}</tbody></table>${empty}${more}`;
}

/* An import event carries the whole search path as arguments. The module name
   is the part a reviewer needs; the rest is noise wide enough to break the table. */
function target(e) {
  if (e.path) return e.path;
  const first = e.event.startsWith("import") ? e.args.slice(0, 1) : e.args;
  const text = first.filter((a) => a && a !== "None").join(", ");
  return text.length > 160 ? text.slice(0, 160) + "..." : text;
}

function closure(p) {
  const rows = p.closure.map((f) => `
    <tr><td class="mono">${esc(f.path)}</td><td>${esc(f.reason)}</td>
        <td class="mono">${esc(f.sha256)}</td><td class="mono">${f.bytes} B</td></tr>`).join("");
  return `<table class="grid">
    <thead><tr><th>file</th><th>why it was copied</th><th>sha256</th><th>size</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function decideBar(p) {
  return `<div class="decide">
    <div class="prompt">Your call on <strong>${esc(p.request_id)}</strong>. Approve commits it to the real file; reject destroys the sandbox.</div>
    <input type="text" id="reason" placeholder="reason (optional, recorded in the audit log)" autocomplete="off">
    <button class="reject" type="button">Reject<kbd>R</kbd></button>
    <button class="approve" type="button">Approve<kbd>A</kbd></button>
  </div>`;
}

async function decide(approved) {
  if (submitting || !currentRequest) return;
  submitting = true;
  document.querySelectorAll(".decide button").forEach((b) => (b.disabled = true));
  const reason = ($("reason") && $("reason").value) || "";
  try {
    await api("/api/decision", {
      method: "POST",
      body: JSON.stringify({ request_id: currentRequest, approved, reason }),
    });
  } catch (err) {
    submitting = false;
    document.querySelectorAll(".decide button").forEach((b) => (b.disabled = false));
  }
}

/* ---------- results ---------- */

function drawResults(records, summary) {
  $("results").hidden = false;
  const rows = records.map((r) => `
    <tr>
      <td class="mono">${esc(r.request_id)}</td>
      <td>${esc(r.title)}</td>
      <td class="mono">${esc(r.verdict || "—")}</td>
      <td><span class="out ${esc(r.outcome)}">${esc(r.outcome)}</span></td>
      <td class="mono">${esc(r.rules.join(", ") || r.error || "")}</td>
      <td class="mono">${esc(r.commit || "")}</td>
    </tr>`).join("");
  const head = summary
    ? `<p class="note">${summary.approved} approved · ${summary.rejected} rejected · ${summary.errored} errored
       · baseline <span class="rule">${esc(summary.baseline_sha)}</span></p>` : "";
  $("results-body").innerHTML = head + `<table class="grid">
    <thead><tr><th>request</th><th>title</th><th>verdict</th><th>outcome</th><th>rules / error</th><th>commit</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

/* ---------- keyboard ---------- */

$("close-btn").addEventListener("click", async () => {
  $("close-btn").disabled = true;
  $("close-btn").textContent = "Closing...";
  try { await api("/api/close", { method: "POST", body: "{}" }); } catch (err) { /* it is going away */ }
});

document.addEventListener("keydown", (event) => {
  if (!currentRequest || event.metaKey || event.ctrlKey || event.altKey) return;
  if (document.activeElement && document.activeElement.tagName === "INPUT") return;
  const key = event.key.toLowerCase();
  if (key === "a") decide(true);
  if (key === "r") decide(false);
});

drawSteps();
poll();
