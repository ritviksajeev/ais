"""A live-model editor agent.

This is the component the whole project is built to distrust, finally made
real: instead of reading fixed edits from a YAML file, it asks a model to
produce them. It emits exactly the same :class:`~ais.models.EditRequest` the
scripted editor does -- project-relative paths and content, no handle, no path
capability -- so nothing downstream can tell, or needs to tell, that the edits
came from a model. That is the point of putting the trust boundary here.

Reproducibility is preserved by the :class:`~ais.llm.Transport`: the same task
run in replay mode returns the same recorded proposal every time, so a
model-driven ``--eval`` produces a stable, auditable number.
"""

from __future__ import annotations

from typing import Iterator

from ais.editor.scripted import EditorError
from ais.llm import EditTask, LlmError, Transport
from ais.models import EditRequest


class ModelEditor:
    """Turns a fixed list of :class:`EditTask` into requests via a transport.

    The task list is fixed; the *responses* come from a model. That split is
    what keeps the evaluation honest: the adversarial pressure (or its absence)
    is authored and auditable, while the behaviour under test -- what the model
    does with it -- is the live, unpredictable part.
    """

    def __init__(self, transport: Transport, tasks: list[EditTask]) -> None:
        self.transport = transport
        self.tasks = tasks
        self._requests: list[EditRequest] | None = None

    def requests(self) -> list[EditRequest]:
        if self._requests is None:
            self._requests = [self._to_request(task) for task in self.tasks]
        return self._requests

    def __iter__(self) -> Iterator[EditRequest]:
        return iter(self.requests())

    def __len__(self) -> int:
        return len(self.requests())

    def select(self, only: list[str] | None = None) -> list[EditRequest]:
        """All requests, or just those whose id matches a pattern (substring)."""
        if not only:
            return self.requests()
        chosen: list[EditRequest] = []
        for pattern in only:
            matches = [r for r in self.requests() if pattern in r.request_id]
            if not matches:
                known = ", ".join(t.task_id for t in self.tasks)
                raise EditorError(f"no task matches {pattern!r}. Known ids: {known}")
            chosen.extend(m for m in matches if m not in chosen)
        return chosen

    def _to_request(self, task: EditTask) -> EditRequest:
        try:
            proposals = self.transport.propose(task)
        except LlmError as exc:
            # Surface as an editor error so demo.py reports it as a user-facing
            # message rather than a stack trace, exactly like a scripted error.
            raise EditorError(str(exc)) from exc

        if not proposals:
            # The model changed nothing (or refused). Represent that honestly as
            # a request that proposes each target's current content unchanged;
            # the pipeline will see a no-op diff and pass it.
            proposed = dict(task.files)
            rationale = "model proposed no change"
        else:
            proposed = {p.path: p.content for p in proposals}
            rationale = "; ".join(p.summary for p in proposals if p.summary) or task.instruction

        targets = tuple(sorted(proposed))
        return EditRequest(
            request_id=task.task_id,
            title=task.title,
            rationale=rationale,
            targets=targets,
            proposed=proposed,
            expected=task.expected,
            expect_rules=task.expect_rules,
            note=task.note,
        )
