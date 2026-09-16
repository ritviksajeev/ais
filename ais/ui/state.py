"""The shared state between the pipeline thread and the browser.

The pipeline runs on a worker thread and blocks at the review step; the HTTP
server answers polls on its own threads. Everything they share lives here,
behind one lock, and is only ever exchanged as plain JSON-ready dicts.

That last part is deliberate rather than incidental. The SQLite connection
behind the audit log belongs to the thread that opened it, and a
:class:`~ais.models.SandboxPlan` carries the real project root -- the one path
in the system an editor agent must never see. Converting at the boundary means
the HTTP layer holds no handle it could misuse and no path it could leak, so
the UI is a viewer over the run rather than a second way into it.
"""

from __future__ import annotations

import threading
from typing import Any

from ais.models import Decision, Stage

#: What each pipeline stage means, in the words of someone who has not read the
#: source. The UI's purpose is to make the mechanism legible, so the plain
#: description is part of the product rather than a tooltip afterthought.
STAGE_COPY: dict[str, dict[str, str]] = {
    Stage.REQUEST_RECEIVED.value: {
        "component": "Editor",
        "title": "Edit proposed",
        "blurb": "The agent submitted file contents and a rationale. It was given "
        "no real path and no file handle -- it cannot write anything itself.",
    },
    Stage.PLAN_BUILT.value: {
        "component": "Mediator",
        "title": "Closure computed",
        "blurb": "Worked out the smallest set of files this edit needs to run: the "
        "targets, what they import, and the tests that cover them.",
    },
    Stage.PLAN_REJECTED.value: {
        "component": "Mediator",
        "title": "Refused",
        "blurb": "The request failed a check before any sandbox existed. Nothing "
        "was copied and nothing was executed.",
    },
    Stage.SANDBOX_MATERIALIZED.value: {
        "component": "Mediator",
        "title": "Sandbox built",
        "blurb": "Copied those files into a fresh disposable workspace. The real "
        "project is untouched from here until a human approves.",
    },
    Stage.DIFF_COMPUTED.value: {
        "component": "Mediator",
        "title": "Diff computed",
        "blurb": "Compared the proposal against the real file to produce the exact "
        "patch under review.",
    },
    Stage.SANDBOX_EXECUTED.value: {
        "component": "Sandbox",
        "title": "Code executed",
        "blurb": "Applied the patch inside the sandbox and ran the tests, with no "
        "network and hard limits on time and memory, watching every file open, "
        "socket and subprocess.",
    },
    Stage.VERIFIED.value: {
        "component": "Verifier",
        "title": "Behaviour checked",
        "blurb": "Ran the rule set over what was actually observed -- not over the "
        "text of the diff -- and recommended a verdict.",
    },
    Stage.DECIDED.value: {
        "component": "Reviewer",
        "title": "Human decided",
        "blurb": "A person read the diff and the execution report and made the call. "
        "The verdict above is advice; this is the decision.",
    },
    Stage.APPLIED.value: {
        "component": "Mediator",
        "title": "Committed",
        "blurb": "Approved, so the patch was written to the real file as a git "
        "commit -- attributable, and revertible with one command.",
    },
    Stage.DISCARDED.value: {
        "component": "Mediator",
        "title": "Discarded",
        "blurb": "Rejected, so the sandbox was destroyed. The real files were never "
        "written to at all.",
    },
    Stage.ERRORED.value: {
        "component": "Pipeline",
        "title": "Error",
        "blurb": "The run could not continue. The log records how far it got.",
    },
}


def describe_stage(stage: str) -> dict[str, str]:
    return STAGE_COPY.get(
        stage, {"component": "Pipeline", "title": stage.replace("_", " "), "blurb": ""}
    )


class UiState:
    """Everything the browser can see, and the one thing it can change."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._seq = 0
        self._run: dict[str, Any] = {}
        self._pending: dict[str, Any] | None = None
        self._records: list[dict[str, Any]] = []
        self._status = "starting"
        self._message = "Starting up"
        self._summary: dict[str, Any] | None = None
        #: Set by the browser posting a decision; awaited by the review thread.
        self._decision: Decision | None = None
        self._decision_ready = threading.Event()
        #: Set when the browser asks to stop, or the window is closed.
        self.aborted = threading.Event()
        #: Set when the human is finished reading the results and closes the
        #: page. The run is over by then; this only ends the server.
        self.closed = threading.Event()

    # -- written by the pipeline thread ------------------------------------

    def set_run(self, **fields: Any) -> None:
        with self._lock:
            self._run.update(fields)

    def set_status(self, status: str, message: str = "") -> None:
        with self._lock:
            self._status = status
            if message:
                self._message = message

    def add_event(self, event: dict[str, Any]) -> None:
        """Record one audit event for display. Called from the audit log."""
        # ``component`` is the display label for the stage; ``actor`` is the
        # audit log's own answer to "who did this" (``reviewer:web``,
        # ``mediator``, ``safety``). The label must not overwrite the record.
        described = describe_stage(event.get("stage", ""))
        with self._lock:
            self._seq += 1
            self._events.append(
                {
                    "seq": self._seq,
                    "stage": event.get("stage", ""),
                    "request_id": event.get("request_id"),
                    "ts": event.get("ts", ""),
                    "actor": event.get("actor", ""),
                    "hash": (event.get("hash") or "")[:12],
                    **described,
                    "actor": event.get("actor", ""),
                }
            )

    def add_record(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._records.append(record)

    def set_summary(self, summary: dict[str, Any]) -> None:
        with self._lock:
            self._summary = summary

    # -- the review handshake ----------------------------------------------

    def await_decision(self, presentation: dict[str, Any]) -> Decision | None:
        """Publish a request for review and block until the browser answers.

        Returns ``None`` if the run was aborted instead of decided.
        """
        with self._lock:
            self._pending = presentation
            self._status = "awaiting_decision"
            self._message = f"Waiting for your decision on {presentation['request_id']}"
            self._decision = None
        self._decision_ready.clear()

        while not self._decision_ready.wait(timeout=0.2):
            if self.aborted.is_set():
                with self._lock:
                    self._pending = None
                return None

        with self._lock:
            decision = self._decision
            self._pending = None
            self._status = "running"
            self._message = "Working"
        return decision

    def submit_decision(self, decision: Decision) -> bool:
        """Called from an HTTP thread. False if nothing was waiting."""
        with self._lock:
            if self._pending is None:
                return False
            if self._pending["request_id"] != decision.request_id:
                return False
            self._decision = decision
        self._decision_ready.set()
        return True

    def abort(self) -> None:
        self.aborted.set()
        self._decision_ready.set()

    def close(self) -> None:
        self.closed.set()

    def release(self) -> None:
        """Wake a waiting reviewer without declaring the run aborted."""
        self._decision_ready.set()

    # -- read by the HTTP threads ------------------------------------------

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "message": self._message,
                "run": dict(self._run),
                "events": [e for e in self._events if e["seq"] > since],
                "seq": self._seq,
                "pending": self._pending,
                "records": list(self._records),
                "summary": self._summary,
            }
