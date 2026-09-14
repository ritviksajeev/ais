# Changelog

All notable changes to AiS are recorded here.

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

[0.1.0-alpha]: https://github.com/ritviksajeev/ais/releases/tag/v0.1.0-alpha
