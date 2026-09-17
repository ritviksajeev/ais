# Changelog

All notable changes to AiS are recorded here.

## [Unreleased]

### Added
- **Differential behavioural verification — a second oracle.** The test suite
  used to be the only thing AiS could judge correctness against, which made it
  exactly as good as those tests: a wrong value on an input nothing exercises
  was invisible, and this file previously called that a structural limit. It was
  not. The previous version of the code is an oracle too.

  `ais/sandbox/differ.py` uses it. The runner copies the workspace aside before
  applying the patch, then calls every public function on both versions with the
  same generated arguments and records what came back. Any call that answers
  differently is a behaviour change on an input nobody had to think to write
  down. The two sides are probed in separate processes: modules import each
  other by name, and loading both versions in one interpreter would quietly mix
  a baseline module with a patched one.

  Two new rules. `behaviour.diverged` fires only when the edit changes behaviour
  **and leaves every test file alone** — a change that updates its tests is a
  *declared* one, and reported as context rather than as a finding. The
  rounding-fix scenario diverges on `round_half_up(-0.5)` on purpose and stays a
  clean pass. `behaviour.probe_absent` says so when the comparison could not
  run, because an unchanged result is otherwise indistinguishable from an
  unattempted one.

  The red team's `logic-uncovered` attack is now caught, taking the campaign from
  80% to **85%**, with false positives still at zero and the ten scenarios
  unchanged at 100%/100%/0%. `clean-03` and the large `format_cents` rewrite
  produce zero divergences — those refactors are now demonstrably
  behaviour-preserving, which is a stronger claim than "the tests still pass".

  One lesson worth recording: the first candidate input list used `100_000` and
  `250_000` and missed the planted bug entirely, because with no cents to drop
  both sides render `"1000.00"`. A probe only finds a difference where the
  difference is visible, so the values have to be ragged in whatever way the
  code divides on, not merely large. A test pins that property.
- **The red-team catalogue grew from 16 attacks to 24**, adding questions the
  first campaign never asked: exfiltration through DNS resolution alone,
  payloads deferred to `atexit` and to `__del__`, a dangerous name assembled at
  runtime via `getattr`, the low-level `os.open` door, a symlink out of the
  workspace, a change that is merely *slow*, and a benign `pathlib` read as the
  control for having widened the denylist.

  The score went **down**, from 92% to **80% detection (16/20)**, with false
  positives still at zero across four benign probes. That is what a working red
  team does. Three new gaps, none of them previously known:
  - `getattr(os, "rem" + "ove")` in dead code — the dangerous name never appears
    in the AST and the code never runs, so there is nothing to match and nothing
    to observe. This is the honest limit of a static denylist, and it is not
    closeable by naming things.
  - dormant `os.open(...)` — one rung below the names the scan knows.
  - six seconds burned on import — the wall-clock rule only fires at the
    ceiling, and nothing compares a run against the previous one. A whole
    missing rule class rather than a missing name.

  Confirmed working, rather than broken: DNS-only exfiltration, both
  deferred-execution tricks and the symlink escape are all caught. Observation
  does not stop when the test run does.

### Fixed
- **Generation crashed on a CRLF checkout.** Attacks are authored with `\n`
  anchors, but a clone made before this repository pinned `*.py` to LF still has
  CRLF on disk, and `git pull` does not renormalise files a pull did not
  otherwise touch — so an ordinary working copy could be CRLF while the anchors
  were LF. Every multi-line anchor matched nothing and `--redteam` died with
  `InjectionError`. Injection is now line-ending aware: it reads the file's own
  convention, translates the anchor to it, and preserves it in the result, so
  the proposed content stays in the same endings as the baseline it will be
  diffed against.
- `finalizer-payload` was scored as a miss it had not earned. Kept alive as a
  module global, its `__del__` only ran during interpreter shutdown — after
  builtins were torn down, where it died on `NameError: name 'open' is not
  defined`. The attack never executed, so counting it against the Verifier
  understated detection by blaming a rule for something that never happened. An
  attack that cannot execute is not evidence. It now drops the object during the
  run, and is caught. A test pins that, and another asserts no attack in the
  catalogue proposes a file unchanged.

## [0.1.3-alpha] — 2026-09-16

### Fixed
- **The write allowlist was destroyed on Windows.** `runner.py` builds
  `AIS_WRITE_ALLOWLIST` with `os.pathsep.join`, which is `;` on Windows, but the
  tracer split it on a hardcoded `":"`. Every path was torn apart at its drive
  letter — `C:\Users\...` became `["C", "\Users\..."]` — so nothing was
  allowlisted, the interpreter's own ordinary writes registered as escapes, and
  a completely benign edit came back flagged. The sandbox container is always
  Linux, so this only ever bit `--backend local`, and nothing in the suite
  exercised that backend until the red team became the first test to run it for
  real. Parsing is now a named function with the join/split symmetry pinned by a
  test.

### Changed
- **Four of the five gaps the first red-team campaign found are closed**, taking
  the same campaign from 62% detection to **92%**, with false positives still at
  zero and the ten-scenario evaluation unchanged at 100%/100%/0%:
  - the static denylist learned `shutil`'s copiers, `os.rename`/`replace`/
    `link`/`symlink`/`truncate`, and pathlib's mutators (`unlink`, `rmdir`,
    `write_text`, `write_bytes`) — the last matched on the method name, because
    `Path(x).unlink()` reaches the call through a receiver a dotted-name walk
    cannot reconstruct;
  - `open(...)` is now read for its *mode*: a write, append or create is
    flagged, an ordinary read stays quiet, and a mode computed at runtime is
    reported rather than assumed harmless;
  - `tests.oracle_weakened` now counts a newly added `skip`/`xfail` marker as
    weakening. A skip strips a test of its power to judge without deleting a
    line, so a rule watching only for removals never saw it. A skip on its own
    reports MEDIUM rather than HIGH: `skipif(sys.platform == "win32")` is a
    legitimate thing to add, and blocking it outright is the kind of false
    positive that gets a tool switched off. A deletion still blocks.
- The four closed attacks stay in the red-team catalogue as regression cover and
  now predict `caught`, so a future change that drops one reports as *slipped
  through* rather than passing quietly.
- The one remaining slip — a wrong value on an input range no test exercises —
  is pinned open by a test. It is structural, not an oversight: for correctness,
  AiS is exactly as good as the test suite it runs.

### Added
- **Phase 0, the red team: `python demo.py --redteam`.** Generates adversarial
  edits against the sample project rather than against the rule list, runs them
  blind through the same pipeline, and scores them with the same ground-truth
  machinery the ten scenarios use — but, unlike `--eval`, it is meant to fail.
  Its first run scored 62% detection with zero false positives and five
  reproducible slips; four of those are closed above, and the campaign now
  scores 92%. Naming a gap precisely is the point: it turns "100% on ten
  hand-written scenarios" from a boast into a measurement.

  The catalogue (`ais/redteam/library.py`) also carries attacks that *should* be
  caught — obfuscated or aliased calls that still execute, and denylisted
  constructs in dead code — which confirm the tracer and static scan do what they
  claim. Generation is deterministic in its seed, and the strategy is a seam: a
  `ModelStrategy` backed by a live LLM would implement the same one method with
  nothing downstream changing. The report is written to `REDTEAM.md`.

- **A browser review surface: `python demo.py --ui`.** The same pipeline, with
  the decision moved out of the terminal and into a local page — a rail naming
  each of the seven steps in plain English and lighting them as they happen, a
  live feed of what each component just did and why, and the request itself with
  its diff, findings, test results and the operations the edit performed while
  it ran. The terminal shows the evidence; the page shows the mechanism.

  Standard library only: no framework, no build step. It binds `127.0.0.1` and
  never `0.0.0.0`, and the URL carries a token generated per run, because
  "localhost" still includes whatever agent is being mediated. The page is
  rendered from a JSON packing that omits `plan.project_root`, so the real
  location of the files never reaches the browser.

  Adding it moved no Mediator, Sandbox or Verifier code, which is what the
  `Reviewer` seam was for.
- `AuditLog.subscribe()`: call a listener with each event as it is appended. The
  live view is a second reader of the hash-chained log rather than a parallel
  event channel. A listener that raises is swallowed — a display problem must
  never become an audit problem.

### Changed
- **The review UI answers one question at a time.** The first version put
  everything on screen at once — a seven-step rail, a live event feed, and six
  expanded panels — which is a lot to read before deciding anything. A request
  now shows what the edit is, one plain-English sentence for whether something
  is wrong with it, and two buttons; the findings, diff, test results, execution
  trace and sandbox contents sit behind collapsed drawers. Nothing was removed.
- `ais/ui/plain.py` renders the Verifier's findings into that sentence. It is a
  lead, not a substitute: a rule with no phrase falls back to its title rather
  than being dropped, and a test asserts every rule in the catalogue has one.
- The UI now uses the evzero.org design language — near-black grounds, a single
  purple, sharp radii, mono uppercase labels — and makes no outbound request,
  fonts included.
- Approve and Reject are the same visual weight. Styling approve as the primary
  action would make approving the path of least resistance.

### Fixed
- The mediated repository translated line endings on Windows. `.ais_run/project`
  is its own `git init` repo with no `.gitattributes`, so git's default
  `core.autocrlf=true` applied to it and every `git checkout` rewrote LF to CRLF
  on the way out of the object database. Since the Mediator checks out a fresh
  branch per request, the bytes the sandbox executed, the human reviewed and the
  audit log hashed were not the bytes that had been committed — on one platform
  only. `ensure_repo` now pins `core.autocrlf=false` and `core.eol=lf` on every
  open, including repositories left behind by earlier runs.

## [0.1.2-alpha] — 2026-09-16

### Fixed
- **`--backend local` would carry out the destructive scenarios for real.** The
  local backend was documented as not being an isolation boundary, but nothing
  enforced that: running scenario 05 through it deleted the files it names, and
  on Windows `os.path.expanduser("~/.ssh/known_hosts")` is a real path with a
  real file behind it. A warning in a README is not a control.

  `ais/safety.py` now matches a request's risk against the containment actually
  available and refuses the pairing that cannot survive — before a sandbox
  exists, as a pre-flight rather than a verdict, so it is never reported as a
  finding about the edit. Scenarios 05, 06 and 09 are refused on a non-isolating
  backend with an explanation naming the construct and the line; the other seven
  run unchanged. `--allow-uncontained` overrides it deliberately.
- `--eval` now requires a real isolation boundary. Detection numbers from a
  backend that contains nothing do not mean what the results table says, and
  producing them meant executing several scenarios for real.
- The README told people to run `pytest`, which is not on `PATH` after a pip
  install on Windows. Everything now uses `python -m pytest`, and CI runs the
  same command so the documented one is the tested one.

### Changed
- The `sandbox.not_isolated` advisory names what the platform can actually
  enforce, so a report from a Windows host does not imply rlimits that are not
  there.

## [0.1.1-alpha] — 2026-09-14

### Fixed
- **AiS could not start on Windows at all.** `ais/sandbox/local_backend.py` and
  `ais/sandbox/runner.py` imported `resource` at module scope. That module is
  POSIX-only, and `demo.py` imports the local backend unconditionally while
  wiring the pipeline together — so every invocation died with
  `ModuleNotFoundError: No module named 'resource'` three frames deep, before
  argument parsing, no matter which backend was requested. The Docker backend
  was unreachable on Windows despite Docker Desktop working fine.

### Added
- `ais/sandbox/procutil.py` — one cross-platform implementation of the process
  and resource handling both the host and the in-sandbox runner need: spawning
  a separately-killable child, killing a process tree, applying POSIX rlimits
  where they exist, and measuring child CPU and peak memory where that is
  possible. It reports a missing capability rather than faking a number, and
  ships into the sandbox alongside `patchkit`.
- CI now runs on **Windows and macOS** as well as Linux, across Python 3.11 and
  3.12, plus a `demo.py --rules` smoke check that exercises every import. The
  original bug was an import error, which a test suite that never imported the
  module on that platform could not have caught.
- `tests/test_platform.py` — makes `resource` unimportable, exactly as Windows
  does, then imports the whole package from scratch. Also asserts no host module
  imports it unguarded, so this cannot regress silently.

### Fixed (second pass, found by the new Windows CI job)
- **The second `python demo.py` run failed on Windows.** The Mediator re-seeds
  its project repository on every run via `shutil.rmtree`, but git marks
  everything under `.git/objects` read-only and Windows refuses to unlink a
  read-only file. POSIX only consults the parent directory's permissions, which
  is why this never appeared in local testing. `procutil.rmtree` now clears the
  read-only bit and retries.
- Two tests mixed `Path.write_text` (which translates `\n` to `\r\n` on
  Windows) with byte-exact reads, so a CRLF checkout produced context that did
  not match its own generated patch. They now use `write_text_exact` /
  `read_text_exact` throughout — the helpers that exist precisely for this.

### Changed
- The write allowlist now includes the host's own temp directory. Under the
  local backend on Windows that is somewhere beneath `AppData`, and without it
  every run reported its own temporary files as escaping the workspace.
- The test suite no longer assumes a POSIX host: `/dev/null` is replaced with an
  in-memory stream, `true` with `sys.executable`, `/etc/hostname` with a file
  the test creates, and the symlink-escape test skips where creating a symlink
  needs administrator rights.
- `LocalSandbox.describe()` now states what the platform can actually enforce,
  so a reviewer reading a report from a Windows host is not told there are
  rlimits when there are none.

## [0.1.0-alpha] — 2026-09-14

First public alpha. The full pipeline works end to end and the evaluation runs.

### Added
- **Mediator** — the only component that touches real files. Refuses
  out-of-scope targets (absolute paths, `..`, `.git/`, symlink escapes) before
  any sandbox is created, computes a per-request import/test closure instead of
  cloning the project, and commits approved diffs on a per-request branch off a
  shared baseline.
- **Docker sandbox** — one disposable container per request: `--network none`,
  capped memory/CPU/PIDs, all Linux capabilities dropped, `no-new-privileges`,
  non-root, and no bind mounts (the bundle is streamed in as a tar and results
  streamed back out).
- **Local fallback backend** — runs the same pipeline without a Docker daemon.
  Explicitly not an isolation boundary, and labelled as such everywhere.
- **Runtime tracer** — a PEP 578 audit hook installed as `sitecustomize`.
  Records attempts rather than outcomes, cannot be uninstalled, survives a
  timeout or OOM kill, and attributes every event to the workspace frame
  responsible.
- **Verifier** — 18 rules covering network egress, writes and reads outside the
  workspace, process spawns, native loading, timeouts, OOM, failing tests,
  rewritten test oracles, undeclared files, denylisted constructs, and a
  tracer-absent guard so an unobserved run can never read as a clean one.
- **Review layer** — UI-agnostic `Reviewer` / `ReviewPresentation` contract with
  a rich CLI showing the diff and execution report side by side, plus a
  non-interactive stand-in for batch runs.
- **Audit log** — append-only SQLite with a SHA-256 hash chain; triggers reject
  `UPDATE` and `DELETE`, and a selective edit is detectable.
- **patchkit** — dependency-free unified-diff engine, shipped into the sandbox
  so the same code applies the patch on both sides of the trust boundary.
- **Evaluation harness** — 10 scripted requests (3 legitimate, 7 planted) with
  ground truth withheld from the Verifier.
- Sample project under edit (73 tests) and 215 tests for AiS itself.

### Known limitations
- Detection numbers come from a fixed, self-authored scenario set. That measures
  internal consistency, not robustness — the adversarial benchmark is the next
  milestone.
- Container escape is out of scope; Docker is trusted as the boundary.
- The tracer observes Python-level operations, so native code called through
  `ctypes` can act beneath it.
- One language, one project shape. Verification is one-shot.

[0.1.3-alpha]: https://github.com/ritviksajeev/ais/releases/tag/v0.1.3-alpha
[0.1.2-alpha]: https://github.com/ritviksajeev/ais/releases/tag/v0.1.2-alpha
[0.1.1-alpha]: https://github.com/ritviksajeev/ais/releases/tag/v0.1.1-alpha
[0.1.0-alpha]: https://github.com/ritviksajeev/ais/releases/tag/v0.1.0-alpha
