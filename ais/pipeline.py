"""The pipeline: request in, audited decision out.

This module is the only place the components meet, and it is deliberately thin
enough to read in one sitting -- the sequence *is* the architecture:

    editor  -> a request with project-relative paths and proposed content
    mediator -> scope check, closure, materialise a sandbox, compute the diff
    sandbox  -> apply the diff to the copy, execute it, observe
    verifier -> rules over the observation, a recommendation
    reviewer -> a human approves or rejects
    mediator -> on approve: apply the diff to the real file, as a git commit
    audit    -> every one of those steps, appended and hash-chained

Every stage is recorded before the next begins, so a crash mid-run leaves a log
that says exactly how far the request got.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Iterable

from ais.audit import AuditLog
from ais.config import Settings
from ais.editor import ScriptedEditor
from ais.mediator import MediationError, Mediator, ScopeViolation
from ais.models import (
    Decision,
    EditRequest,
    ExecutionReport,
    Outcome,
    PipelineRecord,
    Stage,
)
from ais.review.base import Reviewer, ReviewPresentation
from ais.review.cli import ReviewAborted
from ais.sandbox import build_bundle, select_backend
from ais.sandbox.base import SandboxBackend
from ais.verifier import Verifier


@dataclass
class RunSummary:
    """The outcome of pushing a batch of requests through the pipeline."""

    run_id: str
    backend: str
    isolated: bool
    records: list[PipelineRecord]
    baseline_sha: str | None
    isolation_warning: str | None = None
    aborted: bool = False
    #: Why the run stopped early, when it did.
    abort_reason: str | None = None

    @property
    def approved(self) -> list[PipelineRecord]:
        return [r for r in self.records if r.outcome is Outcome.APPROVED]

    @property
    def rejected(self) -> list[PipelineRecord]:
        return [r for r in self.records if r.outcome is Outcome.REJECTED]

    @property
    def errored(self) -> list[PipelineRecord]:
        return [r for r in self.records if r.outcome is Outcome.ERROR]


class Pipeline:
    """Drives one request, or a batch of them, end to end."""

    def __init__(
        self,
        settings: Settings,
        reviewer: Reviewer,
        backend: SandboxBackend | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.settings = settings
        self.reviewer = reviewer
        self.isolation_warning: str | None = None
        if backend is None:
            backend, self.isolation_warning = select_backend(settings)
        self.backend = backend
        self.mediator = Mediator(settings)
        self.verifier = Verifier(settings)
        self._audit = audit
        self._owns_audit = audit is None

    @property
    def audit(self) -> AuditLog:
        if self._audit is None:
            self._audit = AuditLog(self.settings.paths.audit_db)
        return self._audit

    def close(self) -> None:
        if self._audit is not None and self._owns_audit:
            self._audit.close()
            self._audit = None

    # -- batch -------------------------------------------------------------

    def run(self, requests: Iterable[EditRequest], reset: bool = True) -> RunSummary:
        """Push every request through the pipeline in order."""
        self.settings.ensure_dirs()
        baseline = self.mediator.seed(force=reset)

        run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.audit.start_run(
            run_id=run_id,
            backend=self.backend.name,
            isolated=self.backend.isolated,
            baseline_sha=baseline,
            notes=self.isolation_warning or "",
        )

        requests = list(requests)
        records: list[PipelineRecord] = []
        aborted = False
        abort_reason = None

        try:
            # Inside the try: a reviewer that cannot start (no terminal, say) is
            # an orderly end to the run, not an unhandled exception.
            self.reviewer.opening(len(requests))
            for request in requests:
                records.append(self.process(run_id, request))
        except ReviewAborted as exc:
            aborted = True
            abort_reason = str(exc)
        finally:
            self.audit.record(
                run_id,
                Stage.DECIDED if not aborted else Stage.ERRORED,
                "pipeline",
                {
                    "reviewed": len(records),
                    "requested": len(requests),
                    "aborted": aborted,
                    "abort_reason": abort_reason,
                },
            )
            self.audit.finish_run(run_id)

        if records or not aborted:
            self.reviewer.closing([r.decision for r in records if r.decision is not None])

        return RunSummary(
            run_id=run_id,
            backend=self.backend.name,
            isolated=self.backend.isolated,
            records=records,
            baseline_sha=baseline,
            isolation_warning=self.isolation_warning,
            aborted=aborted,
            abort_reason=abort_reason,
        )

    # -- one request -------------------------------------------------------

    def process(self, run_id: str, request: EditRequest) -> PipelineRecord:
        """Run a single request all the way through, recording every stage."""
        started = time.monotonic()
        audit = self.audit
        audit.record(run_id, Stage.REQUEST_RECEIVED, "editor", request.redacted(), request.request_id)

        # Same starting content for every request, on a branch of its own.
        branch = self.mediator.begin_request(request.request_id)

        try:
            plan = self.mediator.plan(request)
        except ScopeViolation as exc:
            # Refused on paperwork: no sandbox is built, nothing is executed.
            audit.record(
                run_id, Stage.PLAN_REJECTED, "mediator", {"error": str(exc)}, request.request_id
            )
            return PipelineRecord(
                request=request,
                outcome=Outcome.ERROR,
                error=f"scope violation: {exc}",
                total_seconds=round(time.monotonic() - started, 3),
            )

        audit.record(
            run_id,
            Stage.PLAN_BUILT,
            "mediator",
            {"closure": plan.describe(), "branch": branch},
            request.request_id,
        )

        try:
            workspace = self.mediator.materialize(plan)
            diff = self.mediator.compute_diff(request)
            audit.record(
                run_id,
                Stage.SANDBOX_MATERIALIZED,
                "mediator",
                {"workspace_files": len(plan.files)},
                request.request_id,
            )
            audit.record(
                run_id,
                Stage.DIFF_COMPUTED,
                "mediator",
                {"diff_sha256": _sha(diff), "diff": diff},
                request.request_id,
            )

            bundle = build_bundle(workspace.parent, diff, self.settings)
            result = self.backend.run(request.request_id, bundle, self.settings)
            audit.record(
                run_id,
                Stage.SANDBOX_EXECUTED,
                "sandbox",
                {
                    "backend": result.backend,
                    "isolated": result.isolated,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "oom_killed": result.oom_killed,
                    "duration_s": result.duration_s,
                    "patch_applied": result.patch_applied,
                    "trace_events": len(result.trace),
                },
                request.request_id,
            )

            report: ExecutionReport = self.verifier.evaluate(request, diff, result)
            audit.record(
                run_id,
                Stage.VERIFIED,
                "verifier",
                {
                    "verdict": report.verdict.value,
                    "anomalies": [a.to_dict() for a in report.anomalies],
                },
                request.request_id,
            )
        except (MediationError, OSError) as exc:
            audit.record(
                run_id, Stage.ERRORED, "pipeline", {"error": str(exc)}, request.request_id
            )
            return PipelineRecord(
                request=request,
                outcome=Outcome.ERROR,
                plan=plan,
                error=f"{type(exc).__name__}: {exc}",
                total_seconds=round(time.monotonic() - started, 3),
            )

        presentation = ReviewPresentation(
            request=request,
            plan=plan,
            diff=diff,
            report=report,
            originals={
                path: self.mediator.read_file(path)
                for path in request.targets
                if (self.mediator.root / path).is_file()
            },
            isolation_warning=self.isolation_warning,
        )

        decision: Decision = self.reviewer.review(presentation)
        audit.record(
            run_id,
            Stage.DECIDED,
            decision.reviewer,
            {
                "approved": decision.approved,
                "reason": decision.reason,
                "review_seconds": decision.review_seconds,
                "verifier_verdict": report.verdict.value,
            },
            request.request_id,
        )

        return self._settle(run_id, request, plan, diff, report, decision, started)

    def _settle(
        self, run_id, request, plan, diff, report, decision, started
    ) -> PipelineRecord:
        """Carry out the decision: commit on approve, destroy the sandbox on reject."""
        if not decision.approved:
            self.mediator.discard(plan)
            self.audit.record(
                run_id,
                Stage.DISCARDED,
                "mediator",
                {"reason": "rejected by reviewer; real files untouched"},
                request.request_id,
            )
            return PipelineRecord(
                request=request,
                outcome=Outcome.REJECTED,
                plan=plan,
                diff=diff,
                report=report,
                decision=decision,
                total_seconds=round(time.monotonic() - started, 3),
            )

        message = (
            f"{request.title}\n\n"
            f"Approved via AiS by {decision.reviewer}.\n"
            f"Request: {request.request_id}\n"
            f"Verifier verdict: {report.verdict.value} "
            f"({', '.join(report.rule_ids) or 'no findings'})\n"
            f"Sandbox: {report.sandbox.backend} "
            f"(isolated={report.sandbox.isolated})\n"
            f"Reason: {decision.reason}\n"
            f"Decided at: {decision.decided_at}"
        )
        try:
            commit = self.mediator.apply(request, diff, message)
        except (MediationError, OSError) as exc:
            self.audit.record(
                run_id,
                Stage.ERRORED,
                "mediator",
                {"error": f"approved but could not apply: {exc}"},
                request.request_id,
            )
            return PipelineRecord(
                request=request,
                outcome=Outcome.ERROR,
                plan=plan,
                diff=diff,
                report=report,
                decision=decision,
                error=f"approved but could not be applied: {exc}",
                total_seconds=round(time.monotonic() - started, 3),
            )

        self.audit.record(
            run_id,
            Stage.APPLIED,
            "mediator",
            {"commit": commit, "files": list(request.targets)},
            request.request_id,
        )
        return PipelineRecord(
            request=request,
            outcome=Outcome.APPROVED,
            plan=plan,
            diff=diff,
            report=report,
            decision=decision,
            commit_sha=commit,
            total_seconds=round(time.monotonic() - started, 3),
        )


def _sha(text: str) -> str:
    from ais.patchkit import sha256_text

    return sha256_text(text)


def load_editor(settings: Settings, scenario_file=None) -> ScriptedEditor:
    """The scripted editor pointed at the default scenario file."""
    from pathlib import Path

    path = scenario_file or (settings.paths.template_project.parent / "scenarios" / "scenarios.yaml")
    return ScriptedEditor(Path(path))
