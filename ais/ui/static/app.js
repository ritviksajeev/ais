/* AiS review UI.
 *
 * The page answers one question at a time. What is on screen when a request
 * arrives is: what the edit is, whether something is wrong with it in one
 * sentence, and two buttons. Every piece of evidence behind that sentence is
 * one click away and none of it is open by default -- a reviewer who wants the
 * trace can always have it, and a reviewer who does not should not have to
 * scroll past it to reach Approve.
 *
 * Everything rendered here came from an edit an agent proposed -- paths,
 * rationales, stdout, trace arguments -- so it is all escaped before it reaches
 * the DOM. A review tool that its own subject could attack would be a poor one.
 */

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const POLL_MS = 350;

/* The seven steps. Shown as seven dots; the words live in the side sheet. */
const STEPS = [
  { who: "Editor",   name: "Edit proposed",
    text: "An agent submits file contents and a reason. It is given no real path and no file handle, so it cannot write anything itself.",
    stages: ["request_received"] },
  { who: "Mediator", name: "Sandbox built",
    text: "Only the files this edit needs are copied into a fresh, disposable workspace — the targets, what they import, and the tests that cover them.",
    stages: ["plan_built", "sandbox_materialized", "plan_rejected"] },
  { who: "Mediator", name: "Diff computed",
    text: "The proposal is compared against the real file to produce the exact patch under review.",
    stages: ["diff_computed"] },
  { who: "Sandbox",  name: "Code executed",
    text: "The patch is applied inside the sandbox and the tests are run, with no network and hard limits on time and memory, while every file open, socket and subprocess is watched.",
    stages: ["sandbox_executed"] },
  { who: "Verifier", name: "Behaviour checked",
    text: "Rules run over what was actually observed, not over the text of the diff, and a verdict is recommended.",
    stages: ["verified"] },
  { who: "You",      name: "Human decides",
    text: "You read the summary and as much of the evidence as you want, and make the call. The verdict is advice; this is the decision.",
    stages: ["decided"] },
  { who: "Mediator", name: "Committed or discarded",
    text: "Approve writes the patch to the real file as a git commit you can revert. Reject destroys the sandbox, and the real file was never written to at all.",
    stages: ["applied", "discarded", "errored"] },
];

const STAGE_TO_STEP = {};
STEPS.forEach((step, i) => step.stages.forEach((s) => (STAGE_TO_STEP[s] = i)));

const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let seq = 0;
let currentId = null;
let currentData = null;
let submitting = false;
let reached = -1;

/* ---------- chrome ---------- */

function drawTrack() {
  $("track").innerHTML = STEPS.map((_, i) =>
    (i ? `<span class="track-line" id="line-${i}"></span>` : "") +
    `<span class="track-dot" id="dot-${i}"></span>`).join("");
}

function markStep(index, active) {
  STEPS.forEach((_, i) => {
    const dot = $(`dot-${i}`);
    const line = $(`line-${i}`);
    dot.classList.toggle("done", i < index || (i === index && !active));
    dot.classList.toggle("active", i === index && active);
    if (line) line.classList.toggle("done", i <= index);
  });
}

function drawSheet() {
  $("sheet-steps").innerHTML = STEPS.map((s) =>
    `<li><h3>${esc(s.who)} — ${esc(s.name)}</h3><p>${esc(s.text)}</p></li>`).join("");
}

/* ---------- polling ---------- */

async function api(path, options = {}) {
  const joiner = path.includes("?") ? "&" : "?";
  const response = await fetch(`${path}${joiner}t=${encodeURIComponent(TOKEN)}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

async function poll() {
  try {
    const state = await api(`/api/state?since=${seq}`);
    seq = state.seq;
    apply(state);
  } catch (err) {
    const done = !$("final").hidden;
    say(done ? "Closed" : "Lost contact",
        done ? "AiS has shut down · this page is now a record"
             : "the AiS process ended or the terminal was closed");
    $("waiting").hidden = false;
    document.querySelector(".pulse").classList.add("still");
    return; // nothing more is coming
  }
  setTimeout(poll, POLL_MS);
}

function say(line, sub) {
  $("waiting-line").textContent = line;
  $("waiting-sub").textContent = sub || "";
}

function apply(state) {
  const run = state.run || {};
  const chip = $("chip-backend");
  if (run.backend) {
    chip.textContent = run.isolated ? "sandboxed" : "no sandbox";
    chip.title = run.backend;
    chip.className = "nav-chip " + (run.isolated ? "good" : "warn");
  }

  if (state.events.length) {
    const last = state.events[state.events.length - 1];
    const index = STAGE_TO_STEP[last.stage];
    if (index !== undefined) {
      reached = last.stage === "request_received" ? 0 : Math.max(reached, index);
      markStep(reached, state.status !== "finished");
    }
    if (!state.pending && state.status === "running") say(last.title, last.blurb ? shorten(last.blurb) : "");
  }

  if (state.pending && state.pending.request_id !== currentId) {
    currentId = state.pending.request_id;
    currentData = state.pending;
    submitting = false;
    $("waiting").hidden = true;
    drawRequest(state.pending);
    markStep(5, true);
  } else if (!state.pending && currentId) {
    currentId = null;
    currentData = null;
    $("request").hidden = true;
    $("request").innerHTML = "";
    $("waiting").hidden = false;
  }

  if (state.status === "finished" || state.status === "aborted") {
    markStep(6, false);
    $("waiting").hidden = true;
    drawFinal(state.records || [], state.summary);
  }
}

const shorten = (text) => {
  const first = text.split(". ")[0];
  return (first.length > 92 ? first.slice(0, 92) + "…" : first).toLowerCase();
};

/* ---------- one request ---------- */

function drawRequest(p) {
  const el = $("request");
  el.hidden = false;
  el.innerHTML = `
    <p class="eyebrow">Request ${p.index} of ${p.total}</p>
    <h1 class="req-title">${esc(p.title)}</h1>
    <p class="req-file">${esc(p.targets.join(" · "))}</p>
    ${answer(p)}
    ${choice()}
    <div class="details">${drawers(p)}</div>`;

  el.querySelector("[data-act=approve]").addEventListener("click", () => decide(true));
  el.querySelector("[data-act=reject]").addEventListener("click", () => decide(false));
  wireTraceToggle(p);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function answer(p) {
  const caveat = p.caveat_lines && p.caveat_lines.length
    ? `<p class="answer-caveat">${esc(p.caveat_lines.join(" "))}</p>` : "";
  return `<div class="answer ${esc(p.verdict)}">
    <p class="answer-lead">${esc(p.lead)}</p>
    <p class="answer-text">${esc(p.summary)}</p>
    ${caveat}
  </div>`;
}

function choice() {
  return `<div class="choice">
    <button class="btn approve" type="button" data-act="approve">Approve <kbd>A</kbd></button>
    <button class="btn reject" type="button" data-act="reject">Reject <kbd>R</kbd></button>
    <input class="reason" id="reason" type="text" autocomplete="off"
           placeholder="reason — optional, kept in the audit log">
  </div>`;
}

/* ---------- the evidence, all collapsed ---------- */

function drawers(p) {
  const x = p.execution;
  const t = x.tests;
  const stats = p.diff_stats || { added: 0, removed: 0 };
  const mine = p.trace.filter((e) => e.from_workspace);
  const escaped = mine.filter((e) => e.escapes_workspace).length;

  return [
    p.findings.length ? drawer(
      "What the Verifier found",
      `${p.findings.length} finding${p.findings.length === 1 ? "" : "s"}`,
      findings(p.findings), "hot") : "",

    drawer("The change", `+${stats.added} −${stats.removed}`, diff(p.diff)),

    drawer("What happened when it ran",
      t ? `${t.passed}/${t.total} tests passed` : "no test result",
      execution(p), t && t.all_passed ? "good" : "hot"),

    p.trace.length ? drawer("What the code did",
      escaped ? `${escaped} left the sandbox` : `${mine.length} operation${mine.length === 1 ? "" : "s"}`,
      `<div id="trace-slot">${trace(p)}</div>`, escaped ? "hot" : "") : "",

    drawer("Files copied into the sandbox", `${p.closure.length} files`, closure(p)),
  ].join("");
}

function drawer(name, meta, body, tone = "") {
  return `<details class="drawer">
    <summary><span class="d-name">${esc(name)}</span><span class="d-meta ${tone}">${esc(meta)}</span></summary>
    <div class="drawer-body">${body}</div>
  </details>`;
}

function findings(list) {
  return list.map((f) => `
    <div class="finding">
      <div class="finding-top">
        <span class="sev ${esc(f.severity)}">${esc(f.severity)}</span>
        <span class="finding-name">${esc(f.title)}</span>
        <span class="finding-rule">${esc(f.rule_id)}</span>
      </div>
      <p>${esc(f.detail)}</p>
      ${f.evidence.length ? `<div class="code">${esc(f.evidence.join("\n"))}</div>` : ""}
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
  const x = p.execution, t = x.tests;
  const facts = [
    fact("tests", t ? `${t.passed}/${t.total}` : "—", t ? (t.all_passed ? "good" : "bad") : ""),
    fact("exit code", x.exit_code === null ? "—" : x.exit_code, x.exit_code === 0 ? "good" : "bad"),
    fact("time", `${x.duration_s}s`, x.timed_out ? "bad" : ""),
    fact("memory", x.max_rss_mb ? `${Math.round(x.max_rss_mb)} MB` : "—", x.oom_killed ? "bad" : ""),
    fact("patch", x.patch_applied ? "applied" : "rejected", x.patch_applied ? "good" : "bad"),
    fact("sandbox", x.isolated ? "container" : "none", x.isolated ? "good" : "warn"),
  ].join("");

  const failing = t && t.failing_tests.length
    ? `<p class="note" style="margin-top:14px">Failing: ${esc(t.failing_tests.join(", "))}</p>` : "";
  const infra = x.infrastructure_error
    ? `<p class="note" style="margin-top:14px;color:var(--warn)">${esc(x.infrastructure_error)}</p>` : "";
  const out = [stream("stdout", x.stdout), stream("stderr", x.stderr)].join("");
  return `<div class="facts">${facts}</div>${failing}${infra}${out}`;
}

const fact = (k, v, tone = "") =>
  `<div class="fact ${tone}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`;

const stream = (name, body) =>
  body && body.trim()
    ? `<details class="drawer" style="margin-top:16px;border-bottom:none">
         <summary><span class="d-name">${esc(name)}</span><span class="d-meta">${body.length} bytes</span></summary>
         <div class="drawer-body"><div class="code">${esc(body)}</div></div>
       </details>` : "";

/* A sandboxed pytest run opens plenty of files that have nothing to do with the
   edit. Defaulting to what the edit itself caused is the difference between a
   table someone reads and a log they scroll past; the rest is one click away. */
function trace(p, showAll = false) {
  const mine = p.trace.filter((e) => e.from_workspace);
  const rows = (showAll ? p.trace : mine).map((e) => {
    const notable = e.escapes_workspace && e.from_workspace;
    return `<tr class="${notable ? "hot" : ""}">
      <td class="mono nowrap">${e.elapsed_ms}ms</td>
      <td class="mono nowrap">${esc(e.event)}</td>
      <td class="mono">${esc(target(e))}</td>
      <td class="nowrap">${notable ? '<span class="tag hot">left sandbox</span>' : ""}</td>
      <td class="mono nowrap">${esc(e.origin || "")}</td>
    </tr>`;
  }).join("");

  const others = p.trace.length - mine.length;
  const toggle = others > 0
    ? `<label class="toggle"><input type="checkbox" id="trace-all" ${showAll ? "checked" : ""}>
         also show the ${others} the test runner did by itself</label>` : "";
  const more = p.trace_total > p.trace.length
    ? `<p class="note" style="margin-top:12px">Showing the first ${p.trace.length} of ${p.trace_total}. The full trace is in the audit log.</p>` : "";
  const empty = !rows ? `<p class="note">The edit itself performed no audited operations.</p>` : "";

  return `${toggle}<div class="scroll"><table>
    <thead><tr><th>at</th><th>operation</th><th>target</th><th></th><th>caused by</th></tr></thead>
    <tbody>${rows}</tbody></table></div>${empty}${more}`;
}

/* An import event carries the whole search path; the module name is the part a
   reviewer needs, and the rest is wide enough to break the table. */
function target(e) {
  if (e.path) return e.path;
  const first = e.event.startsWith("import") ? e.args.slice(0, 1) : e.args;
  const text = first.filter((a) => a && a !== "None").join(", ");
  return text.length > 150 ? text.slice(0, 150) + "…" : text;
}

function wireTraceToggle(p) {
  const box = $("trace-all");
  if (!box) return;
  box.addEventListener("change", () => {
    $("trace-slot").innerHTML = trace(p, box.checked);
    wireTraceToggle(p);
  });
}

function closure(p) {
  const rows = p.closure.map((f) =>
    `<tr><td class="mono">${esc(f.path)}</td><td>${esc(f.reason)}</td><td class="mono nowrap">${f.bytes} B</td></tr>`).join("");
  return `<div class="scroll"><table>
    <thead><tr><th>file</th><th>why it was copied</th><th>size</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

/* ---------- decision ---------- */

async function decide(approved) {
  if (submitting || !currentId) return;
  submitting = true;
  document.querySelectorAll(".choice .btn").forEach((b) => (b.disabled = true));
  const reason = ($("reason") && $("reason").value) || "";
  try {
    await api("/api/decision", {
      method: "POST",
      body: JSON.stringify({ request_id: currentId, approved, reason }),
    });
    say("Working", "");
  } catch (err) {
    submitting = false;
    document.querySelectorAll(".choice .btn").forEach((b) => (b.disabled = false));
  }
}

/* ---------- the end ---------- */

function drawFinal(records, summary) {
  const el = $("final");
  if (!el.hidden) return; // drawn once
  el.hidden = false;
  $("request").hidden = true;

  const s = summary || { approved: 0, rejected: 0, errored: 0 };
  const rows = records.map((r) => `
    <tr>
      <td>${esc(r.title)}</td>
      <td class="mono nowrap">${esc(r.verdict || "—")}</td>
      <td class="nowrap"><span class="outcome ${esc(r.outcome)}">${esc(r.outcome)}</span></td>
      <td class="mono">${esc(r.rules.join(", ") || r.error || "")}</td>
    </tr>`).join("");

  // The headline has to be true of this run: claiming nothing was written when
  // an edit was just committed would be the one lie the page cannot afford.
  const headline = s.approved
    ? `${s.approved} edit${s.approved === 1 ? "" : "s"}<br>committed`
    : "Nothing was<br>written";

  el.innerHTML = `
    <p class="eyebrow">Run complete</p>
    <h1 class="final-title">${headline}</h1>
    <div class="tally">
      <div class="approved"><div class="n">${s.approved}</div><div class="l">approved</div></div>
      <div class="rejected"><div class="n">${s.rejected}</div><div class="l">rejected</div></div>
      ${s.errored ? `<div><div class="n">${s.errored}</div><div class="l">errored</div></div>` : ""}
    </div>
    <div class="details" style="margin-top:38px">
      <div class="scroll"><table>
        <thead><tr><th>edit</th><th>verdict</th><th>outcome</th><th>rules</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
    </div>
    <div class="choice" style="margin-top:34px">
      <button class="btn ghost" type="button" id="close-btn">Close AiS</button>
      <p class="note">Every step is in the audit log — <span class="mono">python demo.py --audit</span></p>
    </div>`;

  $("close-btn").addEventListener("click", async () => {
    $("close-btn").disabled = true;
    $("close-btn").textContent = "Closing…";
    try { await api("/api/close", { method: "POST", body: "{}" }); } catch (err) { /* going away */ }
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ---------- sheet + keys ---------- */

const openSheet  = () => { $("sheet").hidden = false; };
const closeSheet = () => { $("sheet").hidden = true; };

$("how-btn").addEventListener("click", openSheet);
$("sheet-close").addEventListener("click", closeSheet);
$("sheet-scrim").addEventListener("click", closeSheet);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") return closeSheet();
  if (!currentId || event.metaKey || event.ctrlKey || event.altKey) return;
  if (document.activeElement && document.activeElement.tagName === "INPUT") return;
  const key = event.key.toLowerCase();
  if (key === "a") decide(true);
  if (key === "r") decide(false);
});

drawTrack();
drawSheet();
poll();
