"""Wiring: start the server, run the pipeline beside it, hand over the URL.

The pipeline runs on a worker thread and the HTTP server answers on its own,
because the review step blocks by design -- the whole system is built around a
human deciding, and a human takes time. The main thread does nothing but wait
for one of them to finish, so ``Ctrl-C`` still ends the run cleanly.
"""

from __future__ import annotations

import threading
import webbrowser
from typing import Iterable, Sequence

from ais.audit import AuditLog
from ais.config import Settings
from ais.models import EditRequest, Outcome, PipelineRecord
from ais.pipeline import Pipeline, RunSummary
from ais.sandbox.base import SandboxUnavailable
from ais.ui.reviewer import WebReviewer
from ais.ui.server import ReviewServer
from ais.ui.state import UiState


def run_ui(
    settings: Settings,
    requests: Sequence[EditRequest],
    allow_uncontained: bool = False,
    reset: bool = True,
    open_browser: bool = True,
    on_ready=None,
    linger: bool = True,
) -> tuple[int, RunSummary | None]:
    """Review ``requests`` in a browser. Returns an exit code and the summary."""
    settings.ensure_dirs()
    state = UiState()
    server = ReviewServer(state)
    server.start()

    reviewer = WebReviewer(state)
    try:
        pipeline = Pipeline(settings, reviewer, allow_uncontained=allow_uncontained)
    except SandboxUnavailable as exc:
        server.stop()
        raise

    state.set_run(
        backend=pipeline.backend.describe(),
        isolated=pipeline.backend.isolated,
        project=str(settings.paths.live_project),
        total=len(requests),
        isolation_warning=pipeline.isolation_warning,
    )

    holder: dict[str, object] = {}

    def work() -> None:
        try:
            # Opening the log here rather than on the main thread keeps the
            # SQLite connection on the one thread that writes to it.
            audit: AuditLog = pipeline.audit
            audit.subscribe(state.add_event)
            summary = pipeline.run(requests, reset=reset)
            holder["summary"] = summary
            state.set_summary(_summarise(summary))
            for record in summary.records:
                state.add_record(_record(record))
            if summary.aborted:
                state.set_status("aborted", summary.abort_reason or "run aborted")
            else:
                state.set_status("finished", "Run complete")
        except BaseException as exc:  # noqa: BLE001 - surfaced in the UI and re-raised below
            holder["error"] = exc
            state.set_status("error", f"{type(exc).__name__}: {exc}")
        finally:
            pipeline.close()
            # Unblock anything still waiting on a decision. Reached only once
            # the run is over, so it cannot cut a live review short.
            state.release()

    worker = threading.Thread(target=work, name="ais-pipeline", daemon=True)
    worker.start()

    if on_ready is not None:
        on_ready(server.url)
    if open_browser:
        try:
            webbrowser.open(server.url)
        except Exception:  # noqa: BLE001 - a headless host is not a failure
            pass

    try:
        worker.join()
        # The run is over, but the page is the whole point: shutting the server
        # down the instant the last decision lands would mean nobody ever sees
        # the summary. Stay up until the human closes the page or the terminal.
        if linger and not state.aborted.is_set():
            while not state.closed.wait(timeout=0.3):
                pass
    except KeyboardInterrupt:
        state.abort()
        worker.join(timeout=10)
    finally:
        server.stop()

    error = holder.get("error")
    if isinstance(error, BaseException):
        raise error
    summary = holder.get("summary")
    if not isinstance(summary, RunSummary):
        return 2, None
    return 0, summary


def _record(record: PipelineRecord) -> dict:
    report = record.report
    return {
        "request_id": record.request.request_id,
        "title": record.request.title,
        "outcome": record.outcome.value,
        "verdict": report.verdict.value if report else None,
        "rules": list(report.rule_ids) if report else [],
        "commit": (record.commit_sha or "")[:12],
        "error": record.error,
        "seconds": record.total_seconds,
        "reason": record.decision.reason if record.decision else None,
    }


def _summarise(summary: RunSummary) -> dict:
    return {
        "run_id": summary.run_id,
        "backend": summary.backend,
        "isolated": summary.isolated,
        "baseline_sha": (summary.baseline_sha or "")[:12],
        "approved": len(summary.approved),
        "rejected": len(summary.rejected),
        "errored": len(summary.errored),
        "aborted": summary.aborted,
        "abort_reason": summary.abort_reason,
    }
