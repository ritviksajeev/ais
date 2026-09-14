"""The sandbox contract, and the bundle every backend ships.

A backend's job is narrow: take a prepared bundle, execute ``runner.py`` inside
whatever isolation it provides, and hand back what was observed. Everything
about *what* to run, *what* to record and *how* to interpret it lives outside
the backend, so swapping the isolation mechanism does not change a single rule.
"""

from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ais.config import Settings
from ais.sandbox import procutil
from ais.models import SandboxResult, TestSummary, TraceEvent

SANDBOX_PACKAGE = Path(__file__).resolve().parent
AIS_PACKAGE = SANDBOX_PACKAGE.parent


class SandboxUnavailable(RuntimeError):
    """Raised when a backend cannot run here -- no daemon, no image, no permission."""


@dataclass(frozen=True)
class Bundle:
    """Everything a sandbox needs, laid out on disk ready to be shipped in.

    ``root/workspace``  the cloned closure, the only project files in the sandbox
    ``root/ais``        runner, patch engine, tracer and manifest
    ``root/out``        where the sandbox writes its results
    """

    root: Path

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def control(self) -> Path:
        return self.root / "ais"

    @property
    def out(self) -> Path:
        return self.root / "out"


def build_bundle(sandbox_dir: Path, diff: str, settings: Settings) -> Bundle:
    """Assemble a sandbox bundle around an already-materialised workspace."""
    bundle = Bundle(root=sandbox_dir)
    if not bundle.workspace.is_dir():
        raise SandboxUnavailable(f"no workspace materialised at {bundle.workspace}")

    control, out = bundle.control, bundle.out
    if control.exists():
        procutil.rmtree(control)
    (control / "trace").mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)

    shutil.copy2(SANDBOX_PACKAGE / "runner.py", control / "runner.py")
    shutil.copy2(AIS_PACKAGE / "patchkit.py", control / "patchkit.py")
    # The runner imports this directly, so it travels with it.
    shutil.copy2(SANDBOX_PACKAGE / "procutil.py", control / "procutil.py")
    # Named sitecustomize so CPython imports it automatically at startup.
    shutil.copy2(SANDBOX_PACKAGE / "tracer.py", control / "trace" / "sitecustomize.py")
    (control / "change.patch").write_text(diff, encoding="utf-8", newline="")

    limits = settings.limits
    manifest = {
        "command": list(settings.test_command),
        "write_allowlist": list(settings.write_allowlist),
        "limits": {
            "inner_timeout_s": limits.inner_timeout_s,
            "max_output_bytes": limits.max_output_bytes,
            "max_trace_events": limits.max_trace_events,
        },
    }
    (control / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return bundle


class SandboxBackend(ABC):
    """Executes a bundle under isolation and reports what happened."""

    #: Short identifier recorded in every report and audit row.
    name: str = "abstract"
    #: Whether this backend is a real security boundary. The Verifier surfaces
    #: this to the reviewer, because a finding from a non-isolating backend
    #: means "we watched it happen", not "we stopped it happening".
    isolated: bool = False

    @abstractmethod
    def available(self) -> bool:
        """Whether this backend can run on this machine right now."""

    @abstractmethod
    def run(self, request_id: str, bundle: Bundle, settings: Settings) -> SandboxResult:
        """Execute the bundle and return everything observed."""

    def describe(self) -> str:
        return self.name


# --------------------------------------------------------------------------
# reading back what a sandbox produced
# --------------------------------------------------------------------------


def read_trace(out_dir: Path, limit: int) -> tuple[tuple[TraceEvent, ...], bool, bool]:
    """Parse ``trace.jsonl`` into events, tolerating a truncated final line.

    A run killed mid-write leaves a partial last line. That is expected, not an
    error: everything before it is still valid evidence.
    """
    path = out_dir / "trace.jsonl"
    if not path.is_file():
        return (), False, False

    events: list[TraceEvent] = []
    truncated = False
    installed = False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            if len(events) >= limit:
                truncated = True
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                truncated = True  # the run was killed mid-write
                continue
            if record.get("category") == "meta":
                installed = installed or record.get("event") == "tracer.installed"
                continue
            events.append(
                TraceEvent(
                    seq=record.get("seq", len(events) + 1),
                    elapsed_ms=record.get("elapsed_ms", 0),
                    event=record.get("event", "?"),
                    args=tuple(record.get("args", ())),
                    category=record.get("category", "other"),
                    escapes_workspace=bool(record.get("escapes_workspace", False)),
                    path=record.get("path"),
                    write=bool(record.get("write", False)),
                    origin=record.get("origin"),
                    from_workspace=bool(record.get("from_workspace", False)),
                )
            )
    return tuple(events), truncated, installed


def read_result(out_dir: Path) -> dict | None:
    """Parse ``result.json``, or ``None`` if the sandbox never wrote one."""
    path = out_dir / "result.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def assemble_result(
    request_id: str,
    backend: SandboxBackend,
    out_dir: Path,
    settings: Settings,
    duration_s: float,
    *,
    timed_out: bool = False,
    oom_killed: bool = False,
    infrastructure_error: str | None = None,
) -> SandboxResult:
    """Fold a sandbox's raw output into the :class:`SandboxResult` the rules read."""
    raw = read_result(out_dir) or {}
    trace, truncated, tracer_installed = read_trace(out_dir, settings.limits.max_trace_events)

    tests = None
    if isinstance(raw.get("tests"), dict) and raw["tests"].get("collected"):
        summary = raw["tests"]
        tests = TestSummary(
            total=summary.get("total", 0),
            passed=summary.get("passed", 0),
            failed=summary.get("failed", 0),
            errors=summary.get("errors", 0),
            skipped=summary.get("skipped", 0),
            failing_tests=tuple(summary.get("failing_tests", ())),
            collected=True,
        )

    runner_error = raw.get("runner_error")
    if runner_error and not infrastructure_error:
        infrastructure_error = f"in-sandbox runner error: {runner_error}"

    return SandboxResult(
        request_id=request_id,
        backend=backend.name,
        isolated=backend.isolated,
        patch_applied=bool(raw.get("patch_applied", False)),
        patch_error=raw.get("patch_error"),
        exit_code=raw.get("exit_code"),
        timed_out=bool(raw.get("timed_out", False)) or timed_out,
        oom_killed=oom_killed,
        duration_s=round(duration_s, 4),
        stdout=raw.get("stdout", ""),
        stderr=raw.get("stderr", ""),
        trace=trace,
        tests=tests,
        max_rss_mb=raw.get("max_rss_mb"),
        cpu_time_s=raw.get("cpu_time_s"),
        trace_truncated=truncated,
        tracer_installed=tracer_installed,
        infrastructure_error=infrastructure_error,
    )
