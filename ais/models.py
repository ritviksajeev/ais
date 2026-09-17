"""The data that flows between Editor, Mediator, Sandbox, Verifier and Reviewer.

These types are the seams of the system. Every component consumes one of them
and produces another, so a component can be swapped (a real LLM editor, a web
reviewer, a different sandbox backend) without touching its neighbours.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence


def utc_now() -> str:
    """An ISO-8601 timestamp in UTC. Used for every record the pipeline writes."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Severity(str, Enum):
    """How much a single anomaly should worry a reviewer."""

    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class Verdict(str, Enum):
    """The Verifier's advisory recommendation. The human still decides."""

    PASS = "PASS"
    FLAG = "FLAG"
    BLOCK = "BLOCK"


class Outcome(str, Enum):
    """What actually happened to a request at the end of the pipeline."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class Stage(str, Enum):
    """Pipeline stages, in the order the audit log records them."""

    REQUEST_RECEIVED = "request_received"
    PLAN_BUILT = "plan_built"
    PLAN_REJECTED = "plan_rejected"
    SANDBOX_MATERIALIZED = "sandbox_materialized"
    DIFF_COMPUTED = "diff_computed"
    SANDBOX_EXECUTED = "sandbox_executed"
    VERIFIED = "verified"
    DECIDED = "decided"
    APPLIED = "applied"
    DISCARDED = "discarded"
    ERRORED = "errored"


@dataclass(frozen=True)
class EditRequest:
    """An edit an editor agent wants to make.

    Deliberately minimal: logical, project-relative paths and file content. No
    absolute path, no repository handle, no filesystem capability of any kind.
    An editor agent holding one of these cannot touch a real file with it.
    """

    request_id: str
    title: str
    rationale: str
    #: Project-relative paths this request claims to change.
    targets: tuple[str, ...]
    #: Full proposed content, keyed by the same project-relative paths.
    proposed: Mapping[str, str]
    #: Ground-truth label used only by the evaluation harness. The Verifier is
    #: never given this field -- see ``VerificationContext``.
    expected: str = "benign"
    #: Rule ids this scenario was planted to trigger. Ground truth as well: it
    #: lets the evaluation separate "flagged" from "flagged for the right
    #: reason", which are not the same result.
    expect_rules: tuple[str, ...] = ()
    #: Free-form note for the eval write-up, e.g. which rule should catch it.
    note: str = ""

    def redacted(self) -> dict[str, Any]:
        """The view safe to log: metadata without the full file bodies."""
        return {
            "request_id": self.request_id,
            "title": self.title,
            "rationale": self.rationale,
            "targets": list(self.targets),
            "proposed_bytes": {k: len(v) for k, v in self.proposed.items()},
        }


@dataclass(frozen=True)
class ClonedFile:
    """One file the Mediator copied into a sandbox, and why it did."""

    path: str
    reason: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class SandboxPlan:
    """The Mediator's decision about what a request is allowed to see.

    Holds the only real filesystem path in the system. It never leaves the
    Mediator and is never handed to an editor agent.
    """

    request_id: str
    files: tuple[ClonedFile, ...]
    #: Real project root. Mediator-private.
    project_root: Any = None

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"path": f.path, "reason": f.reason, "sha256": f.sha256[:12], "bytes": f.bytes}
            for f in self.files
        ]


@dataclass(frozen=True)
class TraceEvent:
    """One audited runtime operation observed inside the sandbox."""

    seq: int
    elapsed_ms: int
    event: str
    args: tuple[str, ...]
    #: Coarse grouping the rule engine keys off: network, filesystem, process...
    category: str
    #: True when the operation touched a path outside the sandbox workspace.
    escapes_workspace: bool = False
    #: Populated for filesystem events: the path, resolved.
    path: str | None = None
    #: Populated for filesystem events: whether the open was for writing.
    write: bool = False
    #: The nearest source location responsible, e.g. ``textkit.py:42``.
    origin: str | None = None
    #: True when workspace code was on the stack -- that is, when the edit under
    #: review caused this, rather than the test runner's own machinery.
    from_workspace: bool = False

    def summary(self) -> str:
        detail = self.path if self.path else ", ".join(self.args[:2])
        where = f"  <- {self.origin}" if self.origin else ""
        return f"[{self.elapsed_ms:>6}ms] {self.event}({detail}){where}"


@dataclass(frozen=True)
class TestSummary:
    """Structured result of the test command the sandbox ran."""

    #: Not a test class. Stops pytest trying to collect it by name when the
    #: AiS test suite imports it.
    __test__ = False

    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failing_tests: tuple[str, ...] = ()
    collected: bool = True

    @property
    def all_passed(self) -> bool:
        return self.collected and self.failed == 0 and self.errors == 0 and self.total > 0


@dataclass(frozen=True)
class Divergence:
    """One call that behaves differently before and after the edit."""

    call: str
    baseline: str
    proposed: str

    def summary(self) -> str:
        return f"{self.call}: {self.baseline} -> {self.proposed}"


@dataclass(frozen=True)
class SandboxResult:
    """Everything observed during one sandboxed execution."""

    request_id: str
    backend: str
    #: True when the isolation boundary was a real container.
    isolated: bool
    patch_applied: bool
    patch_error: str | None
    exit_code: int | None
    timed_out: bool
    oom_killed: bool
    duration_s: float
    stdout: str
    stderr: str
    trace: tuple[TraceEvent, ...]
    tests: TestSummary | None
    max_rss_mb: float | None = None
    cpu_time_s: float | None = None
    trace_truncated: bool = False
    #: Calls whose result changed between the baseline and the proposed code.
    #: Evidence, not a verdict: plenty of edits change behaviour on purpose.
    divergences: tuple["Divergence", ...] = ()
    #: Set when the behavioural probe could not run or finish.
    diff_probe_error: str | None = None
    #: Whether the runtime audit hook confirmed it was installed. False means
    #: no runtime observation happened at all, so an empty trace says nothing.
    tracer_installed: bool = False
    infrastructure_error: str | None = None

    def events_in(self, *categories: str) -> list[TraceEvent]:
        wanted = set(categories)
        return [e for e in self.trace if e.category in wanted]


@dataclass(frozen=True)
class Anomaly:
    """One finding from the Verifier's rule engine."""

    rule_id: str
    severity: Severity
    title: str
    detail: str
    evidence: tuple[str, ...] = ()
    #: An advisory finding describes the *run*, not the *edit* -- "this backend
    #: is not a real sandbox", "the trace was truncated". It is shown to the
    #: reviewer but never drives the verdict, because otherwise every edit
    #: evaluated in a degraded environment would score as a detection.
    advisory: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "title": self.title,
            "detail": self.detail,
            "evidence": list(self.evidence),
            "advisory": self.advisory,
        }


@dataclass(frozen=True)
class ExecutionReport:
    """What the human is shown alongside the diff."""

    request_id: str
    verdict: Verdict
    anomalies: tuple[Anomaly, ...]
    sandbox: SandboxResult
    generated_at: str = field(default_factory=utc_now)

    @property
    def findings(self) -> tuple["Anomaly", ...]:
        """Anomalies about the edit itself, which are the ones that count."""
        return tuple(a for a in self.anomalies if not a.advisory)

    @property
    def caveats(self) -> tuple["Anomaly", ...]:
        """Anomalies about the run, shown to the reviewer as context."""
        return tuple(a for a in self.anomalies if a.advisory)

    @property
    def max_severity(self) -> Severity | None:
        if not self.findings:
            return None
        return max((a.severity for a in self.findings), key=lambda s: s.rank)

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(a.rule_id for a in self.findings)

    def headline(self) -> str:
        if not self.findings:
            return "no anomalies detected"
        counts: dict[str, int] = {}
        for anomaly in self.findings:
            counts[anomaly.severity.value] = counts.get(anomaly.severity.value, 0) + 1
        order = [s.value for s in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO)]
        return ", ".join(f"{counts[sev]} {sev.lower()}" for sev in order if sev in counts)


@dataclass(frozen=True)
class Decision:
    """A human's (or a stand-in policy's) approve/reject call."""

    request_id: str
    approved: bool
    reviewer: str
    reason: str
    decided_at: str = field(default_factory=utc_now)
    #: Seconds the reviewer spent looking at the request.
    review_seconds: float = 0.0


@dataclass(frozen=True)
class PipelineRecord:
    """The full story of one request, start to finish."""

    request: EditRequest
    outcome: Outcome
    plan: SandboxPlan | None = None
    diff: str = ""
    report: ExecutionReport | None = None
    decision: Decision | None = None
    commit_sha: str | None = None
    error: str | None = None
    total_seconds: float = 0.0

    @property
    def detected(self) -> bool:
        """True when the Verifier recommended anything other than a clean pass."""
        return self.report is not None and self.report.verdict is not Verdict.PASS


def to_json(obj: Any) -> str:
    """Serialise any pipeline dataclass to canonical JSON for the audit log."""
    return json.dumps(_plain(obj), sort_keys=True, separators=(",", ":"), default=str)


def _plain(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)) or (
        isinstance(obj, Sequence) and not isinstance(obj, (str, bytes))
    ):
        return [_plain(v) for v in obj]
    return obj
