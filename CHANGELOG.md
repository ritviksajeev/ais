# Changelog

All notable changes to AiS are recorded here.

## [Unreleased]

### Added
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

[0.1.2-alpha]: https://github.com/ritviksajeev/ais/releases/tag/v0.1.2-alpha
[0.1.1-alpha]: https://github.com/ritviksajeev/ais/releases/tag/v0.1.1-alpha
[0.1.0-alpha]: https://github.com/ritviksajeev/ais/releases/tag/v0.1.0-alpha
