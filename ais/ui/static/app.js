/* AiS review UI -- the "security check" screen.
 *
 * Written for someone who has never heard of a sandbox, a diff or a test
 * suite. When an AI assistant's change arrives, the screen answers three
 * questions in this order and nothing else:
 *
 *   1. What is the AI trying to change?        (title, file, its own reason)
 *   2. Is it safe?                              (one coloured headline line, one sentence)
 *   3. What should I do?                        (two buttons, the safer one highlighted)
 *
 * A short checklist shows *why*, in everyday words. Every piece of technical
 * evidence -- the exact change, the test run, every file and connection the
 * code touched -- is still here, behind "Show technical details", so an IT or
 * security reviewer loses nothing. It is just not in an office worker's way.
 *
 * Everything rendered came from an edit an AI proposed -- paths, reasons,
 * output, trace arguments -- so all of it is escaped before it reaches the
 * page. A security screen that its own subject could attack would be a poor one.
 */

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const POLL_MS = 350;

/* ---------- the four steps a person sees ---------- */

const STEPS = [
  { name: "Suggested",  full: "The AI suggests a change", stages: ["request_received", "plan_built", "plan_rejected"] },
  { name: "Tested",     full: "Tested in a sealed copy", stages: ["sandbox_materialized", "diff_computed", "sandbox_executed"] },
  { name: "Checked",    full: "Checked for risky behaviour", stages: ["verified"] },
  { name: "You decide", full: "You decide", stages: ["decided", "applied", "discarded", "errored"] },
];
const STAGE_TO_STEP = {};
STEPS.forEach((step, i) => step.stages.forEach((s) => (STAGE_TO_STEP[s] = i)));

/* What to say while waiting, by the stage the pipeline just reached. */
const WAITING = {
  request_received:     ["The AI has suggested a change", "Setting up a safe test copy of the files it needs."],
  plan_built:           ["Preparing a safe test copy", "Only the files this change needs are copied."],
  plan_rejected:        ["Change refused before testing", "It asked for something it isn't allowed to touch."],
  sandbox_materialized: ["Safe test copy ready", "Your real files are not involved from here on."],
  diff_computed:        ["Working out exactly what changed", ""],
  sandbox_executed:     ["Testing the change", "Running it in the sealed copy and watching everything it does."],
  verified:             ["Checking what it did", "Looking for internet access, stray files and broken behaviour."],
  decided:              ["Decision recorded", ""],
  applied:              ["Change allowed and saved", "Saved as a normal version you can undo."],
  discarded:            ["Change not allowed", "The test copy was thrown away. Your file was never touched."],
  errored:              ["Something went wrong testing that change", "It was not applied."],
};

/* The "how it works" sheet: the full mechanism, in plain words. */
const HOW = [
  ["The AI only suggests", "The assistant writes its suggested change, but it is never given access to your real files, so it cannot save anything itself."],
  ["A safe copy is made", "AiS copies just the files that change needs into a sealed, throw-away workspace. Your originals stay untouched."],
  ["The change is tested", "The change runs inside that sealed copy, with no internet and strict time and memory limits."],
  ["Everything is watched", "Like anti-cheat in a game, AiS records what the code actually does: every connection, every file it opens, every program it tries to start."],
  ["Compared with before", "The old and new versions are run side by side, so a quiet change in results is caught even if nothing else looks wrong."],
  ["You decide", "You see a plain summary and a recommendation. Allow saves the change as a normal version you can undo. Don't allow throws the test copy away."],
  ["Everything is recorded", "Every step and decision goes into a tamper-evident audit log for IT and compliance."],
];

/* The checklist: each line is a question an office worker would ask, and the
   detection rules that answer "no" to it. Every rule the Verifier knows maps to
   a line; anything new falls through to "Other checks" rather than vanishing. */
const CHECKS = [
  { ok: "Didn't try to connect to the internet",      bad: "Tried to connect to the internet",
    rules: ["net.egress"] },
  { ok: "Only touched the files it was meant to",     bad: "Touched files it had no business touching",
    rules: ["fs.escape_read", "fs.escape_write", "scope.undeclared_file"] },
  { ok: "Didn't start any other programs",            bad: "Tried to start other programs or load hidden code",
    rules: ["proc.spawn", "proc.dynamic_load", "proc.suspicious_import"] },
  { ok: "No hidden risky code",                       bad: "Contains code that can reach outside the program",
    rules: ["code.dangerous_construct"] },
  { ok: "Existing checks still pass",                 bad: "Broke, skipped or rewrote the existing checks",
    rules: ["tests.failed", "tests.not_collected", "tests.oracle_weakened",
            "runtime.timeout", "runtime.memory", "runtime.crash", "patch.rejected"] },
  { ok: "Gives the same results as before",           bad: "Quietly changed the results it gives",
    rules: ["behaviour.diverged"] },
];
const KNOWN_RULES = new Set(CHECKS.flatMap((c) => c.rules));

/* The verdict, per outcome. ``word`` is the coloured second line of the
   headline, so the answer is the biggest thing on the screen; ``recommend`` is
   the button that gets highlighted.
   Only ever the safe one: Allow is never made the path of least resistance,
   because a security check that trains people to click through it has failed.
   A clean result says so in words; the click is still a choice. */
/* When the sealed test area itself failed, the change was never actually
   tested. Calling that "risky" would be a guess dressed as a finding, so it
   gets its own state: say plainly that there is no evidence either way, show
   the reason (for whoever has to fix it), and recommend not allowing. */
const UNTESTED = { tone: "check", icon: "?", word: "Couldn't be tested.",
  advice: "Recommended: don't allow it until the test area is working.", recommend: "reject" };
const untested = (p) => p.findings.some((f) => f.rule_id === "sandbox.infrastructure");

const BANNER = {
  PASS:  { tone: "safe",  icon: "✓", word: "Looks safe.",
           advice: "Nothing risky was found. Allow it if it's a change you expected.", recommend: null },
  FLAG:  { tone: "check", icon: "!", word: "Worth a closer look.",
           advice: "Recommended: have a closer look, or ask IT, before allowing.", recommend: null },
  BLOCK: { tone: "risk",  icon: "✕", word: "Did something risky.",
           advice: "Recommended: don't allow this change.", recommend: "reject" },
};

const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let seq = 0;
let currentId = null;
let submitting = false;
let reached = -1;

const pad = (n) => String(n).padStart(2, "0");
const sectionBar = (left, middle, right, rightClass = "") =>
  `<div class="sec-bar"><span><i class="dot"></i>${left}</span><span>${middle}</span><span class="${rightClass}">${right}</span></div>`;

/* ---------- chrome ---------- */

function drawSteps() {
  $("steps").innerHTML = STEPS.map((s, i) =>
    `<li class="step" id="step-${i}" title="${esc(s.full)}"><span class="nl">${esc(s.name)}</span><sup>${pad(i + 1)}</sup></li>`
  ).join("");
}

function markStep(index, active) {
  STEPS.forEach((_, i) => {
    const el = $(`step-${i}`);
    el.classList.toggle("done", i < index || (i === index && !active));
    el.classList.toggle("active", i === index && active);
  });
}

function drawSheet() {
  $("sheet-steps").innerHTML = HOW.map(([title, text], i) =>
    `<li><p class="sheet-num">${pad(i + 1)}</p><h3>${esc(title)}</h3><p>${esc(text)}</p></li>`).join("");
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
    if (done) return; // the summary stays on screen as a record
    say("Connection lost", "The AiS program has stopped, or its window was closed.");
    $("waiting").hidden = false;
    document.querySelector(".progress").classList.add("still");
    return;
  }
  setTimeout(poll, POLL_MS);
}

function say(line, sub) {
  $("waiting-line").textContent = line;
  $("waiting-sub").textContent = sub || "";
}

function apply(state) {
  const run = state.run || {};
  const pill = $("protection");
  if (run.backend) {
    pill.textContent = run.isolated ? "Sealed test area on" : "No sealed test area";
    pill.title = run.isolated
      ? "Changes are tested inside an isolated container with no internet."
      : "Running without isolation. Only use this for demos.";
    pill.className = "status " + (run.isolated ? "on" : "warn");
  }

  if (state.events.length) {
    const last = state.events[state.events.length - 1];
    const index = STAGE_TO_STEP[last.stage];
    if (index !== undefined) {
      reached = last.stage === "request_received" ? 0 : Math.max(reached, index);
      markStep(reached, state.status !== "finished");
    }
    if (!state.pending && state.status === "running") {
      const [line, sub] = WAITING[last.stage] || [last.title || "Working…", ""];
      say(line, sub);
    }
  }

  if (state.pending && state.pending.request_id !== currentId) {
    currentId = state.pending.request_id;
    submitting = false;
    $("waiting").hidden = true;
    drawRequest(state.pending);
    markStep(3, true);
  } else if (!state.pending && currentId) {
    currentId = null;
    $("request").hidden = true;
    $("request").innerHTML = "";
    $("waiting").hidden = false;
  }

  if (state.status === "finished" || state.status === "aborted") {
    markStep(3, false);
    $("waiting").hidden = true;
    drawFinal(state.records || [], state.summary);
  }
}

/* ---------- one request ---------- */

function drawRequest(p) {
  const el = $("request");
  const failed = untested(p);
  const v = failed ? UNTESTED : (BANNER[p.verdict] || BANNER.FLAG);
  const lead = failed
    ? "The sealed test area failed to run, so AiS has no evidence about what this change does."
    : p.summary;
  const reason = failed && p.execution.infrastructure_error
    ? `<div class="verdict-reason"><span class="label">Reason, for IT</span>${esc(p.execution.infrastructure_error)}</div>` : "";
  const caveat = p.caveat_lines && p.caveat_lines.length
    ? `<p class="verdict-caveat">${esc(p.caveat_lines.join(" "))}</p>` : "";
  el.hidden = false;
  el.innerHTML = `
    ${sectionBar(`(${pad(p.index)} / ${pad(p.total)})`, "(Suggested by the AI assistant)", esc(p.targets.join(", ")))}
    <h1 class="title">${esc(p.title)}<span class="accent ${v.tone}">${esc(v.word)}</span></h1>

    <div class="hero-grid">
      <div>
        ${p.rationale ? `<div class="claim"><span class="label">What the AI says it did</span><p>${esc(p.rationale)}</p></div>` : ""}

        <div class="verdict ${v.tone}">
          <div class="verdict-mark" aria-hidden="true">${v.icon}</div>
          <div class="verdict-body">
            <p class="verdict-text">${esc(lead)}</p>
            <p class="verdict-advice">${esc(v.advice)}</p>
            ${caveat}
            ${reason}
          </div>
        </div>

        <div class="decide">
          <button class="btn allow ${v.recommend === "allow" ? "primary" : ""}" type="button" data-act="approve">
            <span>Allow change<kbd>A</kbd></span><span class="sub">Saved as a version you can undo</span></button>
          <button class="btn reject ${v.recommend === "reject" ? "primary" : ""}" type="button" data-act="reject">
            <span>Don't allow<kbd>R</kbd></span><span class="sub">Your file stays exactly as it is</span></button>
        </div>
        <p class="decide-note">Nothing changes in your files until you click <b>Allow</b>.</p>
        <input class="note-input" id="reason" type="text" autocomplete="off"
               placeholder="Add a note (optional, saved with your decision)">
      </div>

      <div class="term">
        <div class="term-bar"><div class="term-dots"><span></span><span></span><span></span></div>
          <div class="term-title">ais — what we checked</div></div>
        <div class="term-body">
          ${facts(p, failed)}
          <ul class="checklist">${checklist(p)}</ul>
        </div>
      </div>
    </div>

    <details class="tech">
      <summary>${sectionBar("(Details)", "(For IT and security teams)", "", "toggle-word")}</summary>
      <div class="drawers">${drawers(p)}</div>
    </details>`;

  el.querySelector("[data-act=approve]").addEventListener("click", () => decide(true));
  el.querySelector("[data-act=reject]").addEventListener("click", () => decide(false));
  wireTraceToggle(p);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* Three facts at the top of the checklist card, in the AiS page's terminal
   layout but in everyday words. */
function facts(p, failed) {
  const x = p.execution, t = x.tests;
  const stats = p.diff_stats || { added: 0, removed: 0 };
  const row = (k, v, tone = "") => `<div><span class="k">${esc(k)}</span><span class="v ${tone}">${esc(v)}</span></div>`;
  const checks = failed ? ["didn't run", "warn"]
    : t ? [`${t.passed} / ${t.total} passed`, t.all_passed ? "good" : "bad"]
    : ["none ran", "warn"];
  return `<div class="term-facts">
    ${row("test area", x.isolated ? "sealed, no internet" : "not sealed (demo only)", x.isolated ? "" : "warn")}
    ${row("checks", checks[0], checks[1])}
    ${row("changed", `+${stats.added} −${stats.removed} lines`)}
  </div>`;
}

function checklist(p) {
  if (untested(p)) {
    return CHECKS.map((c) =>
      `<li class="unknown"><span class="mark">–</span><span>${esc(c.ok)}<small>not checked: the test didn't run</small></span></li>`).join("");
  }
  const fired = new Map();
  p.findings.forEach((f) => {
    if (!fired.has(f.rule_id)) fired.set(f.rule_id, f);
  });
  const rows = CHECKS.map((c) => {
    const hit = c.rules.map((r) => fired.get(r)).filter(Boolean);
    // The small print adds detail; it never just repeats the line above it.
    const detail = [...new Set(hit.map((f) => f.plain || f.title))].filter((t) => t !== c.bad).join(" · ");
    return hit.length
      ? `<li class="bad"><span class="mark">✕</span><span>${esc(c.bad)}${detail ? `<small>${esc(detail)}</small>` : ""}</span></li>`
      : `<li class="ok"><span class="mark">✓</span><span>${esc(c.ok)}</span></li>`;
  });
  const other = [...fired.values()].filter((f) => !KNOWN_RULES.has(f.rule_id));
  if (other.length) {
    rows.push(`<li class="bad"><span class="mark">✕</span><span>Other checks raised a concern<small>${esc(other.map((f) => f.plain || f.title).join(" · "))}</small></span></li>`);
  }
  return rows.join("");
}

/* ---------- the technical evidence, all collapsed ---------- */

function drawers(p) {
  const x = p.execution;
  const t = x.tests;
  const stats = p.diff_stats || { added: 0, removed: 0 };
  const mine = p.trace.filter((e) => e.from_workspace);
  const escaped = mine.filter((e) => e.escapes_workspace).length;

  return [
    p.findings.length ? drawer(
      "What the checks found",
      `${p.findings.length} finding${p.findings.length === 1 ? "" : "s"}`,
      findings(p.findings), "hot") : "",

    drawer("The exact change", `+${stats.added} −${stats.removed} lines`, diff(p.diff)),

    drawer("What happened when it ran",
      t ? `${t.passed}/${t.total} checks passed` : "no check result",
      execution(p), t && t.all_passed ? "good" : "hot"),

    p.trace.length ? drawer("Everything the code did",
      escaped ? `${escaped} outside its safe area` : `${mine.length} action${mine.length === 1 ? "" : "s"}`,
      `<div id="trace-slot">${trace(p)}</div>`, escaped ? "hot" : "") : "",

    drawer("Files copied into the safe area", `${p.closure.length} files`, closure(p)),
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
  if (!text.trim()) return `<p class="note">No change to the text.</p>`;
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
    fact("checks passed", t ? `${t.passed}/${t.total}` : "—", t ? (t.all_passed ? "good" : "bad") : ""),
    fact("exit code", x.exit_code === null ? "—" : x.exit_code, x.exit_code === 0 ? "good" : "bad"),
    fact("time", `${x.duration_s}s`, x.timed_out ? "bad" : ""),
    fact("memory", x.max_rss_mb ? `${Math.round(x.max_rss_mb)} MB` : "—", x.oom_killed ? "bad" : ""),
    fact("change applied", x.patch_applied ? "yes" : "no", x.patch_applied ? "good" : "bad"),
    fact("sealed area", x.isolated ? "container" : "none", x.isolated ? "good" : "warn"),
  ].join("");

  const failing = t && t.failing_tests.length
    ? `<p class="note">Failing: ${esc(t.failing_tests.join(", "))}</p>` : "";
  const infra = x.infrastructure_error
    ? `<p class="note warn">${esc(x.infrastructure_error)}</p>` : "";
  const out = [stream("output", x.stdout), stream("errors", x.stderr)].join("");
  return `<div class="facts">${facts}</div>${failing}${infra}${out}`;
}

const fact = (k, v, tone = "") =>
  `<div class="fact ${tone}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`;

const stream = (name, body) =>
  body && body.trim()
    ? `<details class="drawer inner">
         <summary><span class="d-name">${esc(name)}</span><span class="d-meta">${body.length} characters</span></summary>
         <div class="drawer-body"><div class="code">${esc(body)}</div></div>
       </details>` : "";

/* A test run opens plenty of files that have nothing to do with the change.
   Showing what the change itself caused is the difference between a table
   someone reads and a log they scroll past; the rest is one click away. */
function trace(p, showAll = false) {
  const mine = p.trace.filter((e) => e.from_workspace);
  const rows = (showAll ? p.trace : mine).map((e) => {
    const notable = e.escapes_workspace && e.from_workspace;
    return `<tr class="${notable ? "hot" : ""}">
      <td class="mono nowrap">${e.elapsed_ms}ms</td>
      <td class="mono nowrap">${esc(e.event)}</td>
      <td class="mono">${esc(target(e))}</td>
      <td class="nowrap">${notable ? '<span class="tag hot">outside safe area</span>' : ""}</td>
      <td class="mono nowrap">${esc(e.origin || "")}</td>
    </tr>`;
  }).join("");

  const others = p.trace.length - mine.length;
  const toggle = others > 0
    ? `<label class="toggle"><input type="checkbox" id="trace-all" ${showAll ? "checked" : ""}>
         also show the ${others} actions the test runner did by itself</label>` : "";
  const more = p.trace_total > p.trace.length
    ? `<p class="note">Showing the first ${p.trace.length} of ${p.trace_total}. The full record is in the audit log.</p>` : "";
  const empty = !rows ? `<p class="note">The change itself did nothing worth recording.</p>` : "";

  return `${toggle}<div class="scroll"><table>
    <thead><tr><th>when</th><th>action</th><th>target</th><th></th><th>caused by line</th></tr></thead>
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
  document.querySelectorAll(".decide .btn").forEach((b) => (b.disabled = true));
  const reason = ($("reason") && $("reason").value) || "";
  try {
    await api("/api/decision", {
      method: "POST",
      body: JSON.stringify({ request_id: currentId, approved, reason }),
    });
    say(approved ? "Saving the change…" : "Discarding the change…", "");
  } catch (err) {
    submitting = false;
    document.querySelectorAll(".decide .btn").forEach((b) => (b.disabled = false));
  }
}

/* ---------- the end ---------- */

const OUTCOME = {
  APPROVED: ["allowed", "Allowed and saved"],
  REJECTED: ["blocked", "Not allowed. Your file was never touched"],
  ERROR:    ["errored", "Couldn't be tested, so not applied"],
};

function drawFinal(records, summary) {
  const el = $("final");
  if (!el.hidden) return; // drawn once
  el.hidden = false;
  $("request").hidden = true;

  const s = summary || { approved: 0, rejected: 0, errored: 0 };
  const rows = records.map((r) => {
    const [cls, words] = OUTCOME[r.outcome] || ["", r.outcome];
    const failed = (r.rules || []).includes("sandbox.infrastructure");
    const tone = failed ? "check" : ((BANNER[r.verdict] || {}).tone || "");
    const verdict = failed ? "Couldn't test"
      : ({ PASS: "Looked safe", FLAG: "Needed a look", BLOCK: "Risky" }[r.verdict] || "—");
    return `<tr>
      <td>${esc(r.title)}</td>
      <td><span class="chip ${tone}">${esc(verdict)}</span></td>
      <td><span class="outcome ${cls}">${esc(words)}</span></td>
    </tr>`;
  }).join("");

  // The headline has to be true of this run: claiming nothing was written when
  // an edit was just saved would be the one lie this page cannot afford.
  const [line, accent] = s.approved
    ? [`${s.approved} allowed,`, `${s.rejected} stopped.`]
    : ["No changes made", "to your files."];
  const stat = (cls, n, label, sub) =>
    `<div class="stat ${cls}"><div class="stat-num">${n}</div><div class="stat-label">${label}</div><div class="stat-sub">${sub}</div></div>`;

  el.innerHTML = `
    ${sectionBar("(Done)", "(Run summary)", `${records.length} change${records.length === 1 ? "" : "s"} reviewed`)}
    <h1 class="title">${esc(line)}<span class="accent">${esc(accent)}</span></h1>
    <div class="stats">
      ${stat("allowed", s.approved, "Allowed", "saved · can be undone")}
      ${stat("blocked", s.rejected, "Not allowed", "files never touched")}
      ${stat("errored", s.errored, "Couldn't test", "not applied")}
    </div>
    <div class="table-wrap"><table class="summary">
      <thead><tr><th>Change</th><th>AiS said</th><th>What happened</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <div class="decide final-actions">
      <button class="btn" type="button" id="close-btn"><span>Close</span><span class="sub">Ends this review session</span></button>
      <p class="note">Every step is in the audit log: python demo.py --audit</p>
    </div>`;

  $("close-btn").addEventListener("click", async () => {
    $("close-btn").disabled = true;
    $("close-btn").firstElementChild.textContent = "Closing…";
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

drawSteps();
drawSheet();
poll();
