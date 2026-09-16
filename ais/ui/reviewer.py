"""A reviewer whose surface is a browser instead of a terminal.

The pipeline is unchanged by this. It still calls ``reviewer.review()`` and
still blocks until a human answers; only the thing being blocked on has moved
from ``input()`` to an HTTP request. That the CLI and the web UI can be swapped
with no other edit is the whole point of keeping the decision layer behind
:class:`~ais.review.base.Reviewer`.
"""

from __future__ import annotations

import time
from typing import Any

from ais.models import Decision
from ais.review.base import Reviewer, ReviewPresentation
from ais.review.cli import ReviewAborted
from ais.ui.state import UiState

#: Trace events are shown in full up to this many, then summarised. A runaway
#: edit can emit tens of thousands; the browser should stay usable, and the
#: complete trace is in the audit log either way.
MAX_TRACE_SHOWN = 300


class WebReviewer(Reviewer):
    """Publishes a request to the UI and waits for the click."""

    name = "reviewer:web"

    def __init__(self, state: UiState) -> None:
        self.state = state
        self._total = 0
        self._seen = 0

    def opening(self, total: int) -> None:
        self._total = total
        self.state.set_run(total=total)
        self.state.set_status("running", f"Reviewing {total} proposed edit(s)")

    def review(self, presentation: ReviewPresentation) -> Decision:
        self._seen += 1
        started = time.monotonic()
        packed = pack_presentation(presentation, index=self._seen, total=self._total)
        decision = self.state.await_decision(packed)
        if decision is None:
            raise ReviewAborted("review window closed before a decision was made")
        return Decision(
            request_id=decision.request_id,
            approved=decision.approved,
            reviewer=self.name,
            reason=decision.reason,
            review_seconds=round(time.monotonic() - started, 2),
        )

    def closing(self, decisions: list[Decision]) -> None:
        self.state.set_status("finished", "Run complete")


def pack_presentation(
    presentation: ReviewPresentation, index: int = 1, total: int = 1
) -> dict[str, Any]:
    """Turn a presentation into JSON the browser can render.

    Note what is *not* here: ``plan.project_root``. The closure is described by
    project-relative path and reason only, so the real location of the files
    never reaches the page.
    """
    request = presentation.request
    report = presentation.report
    sandbox = report.sandbox

    trace = [_event(e) for e in sandbox.trace[:MAX_TRACE_SHOWN]]
    return {
        "request_id": request.request_id,
        "index": index,
        "total": total,
        "title": request.title,
        "rationale": request.rationale,
        "targets": list(request.targets),
        "closure": presentation.plan.describe(),
        "diff": presentation.diff,
        "isolation_warning": presentation.isolation_warning,
        "verdict": report.verdict.value,
        "headline": report.headline(),
        "findings": [a.to_dict() for a in report.findings],
        "caveats": [a.to_dict() for a in report.caveats],
        "execution": {
            "backend": sandbox.backend,
            "isolated": sandbox.isolated,
            "patch_applied": sandbox.patch_applied,
            "patch_error": sandbox.patch_error,
            "exit_code": sandbox.exit_code,
            "timed_out": sandbox.timed_out,
            "oom_killed": sandbox.oom_killed,
            "duration_s": round(sandbox.duration_s, 2),
            "max_rss_mb": sandbox.max_rss_mb,
            "cpu_time_s": sandbox.cpu_time_s,
            "tracer_installed": sandbox.tracer_installed,
            "trace_truncated": sandbox.trace_truncated,
            "infrastructure_error": sandbox.infrastructure_error,
            "stdout": _tail(sandbox.stdout),
            "stderr": _tail(sandbox.stderr),
            "tests": _tests(sandbox.tests),
        },
        "trace": trace,
        "trace_total": len(sandbox.trace),
    }


def _tests(summary) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "total": summary.total,
        "passed": summary.passed,
        "failed": summary.failed,
        "errors": summary.errors,
        "skipped": summary.skipped,
        "collected": summary.collected,
        "all_passed": summary.all_passed,
        "failing_tests": list(summary.failing_tests),
    }


def _event(event) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "elapsed_ms": event.elapsed_ms,
        "event": event.event,
        "category": event.category,
        "path": event.path,
        "write": event.write,
        "escapes_workspace": event.escapes_workspace,
        "from_workspace": event.from_workspace,
        "origin": event.origin,
        "args": list(event.args[:3]),
    }


def _tail(text: str, limit: int = 8000) -> str:
    """Keep the end of a stream: that is where a failure says what went wrong."""
    if len(text) <= limit:
        return text
    return "...(truncated)...\n" + text[-limit:]
