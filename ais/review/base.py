"""What a reviewer is shown, and what a reviewer returns.

Everything a decision needs is packed into :class:`ReviewPresentation` and
everything a decision produces is a :class:`~ais.models.Decision`. The pipeline
knows nothing else about reviewing, which is what lets the review surface change
-- a terminal now, a local web page later, two competing layouts in an A/B study
-- without any of the Mediator, Sandbox or Verifier code moving.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from ais.models import Decision, EditRequest, ExecutionReport, SandboxPlan


@dataclass(frozen=True)
class ReviewPresentation:
    """One request, packaged for a human to judge."""

    request: EditRequest
    plan: SandboxPlan
    diff: str
    report: ExecutionReport
    #: Current content of each target, so the reviewer can see what is being changed.
    originals: Mapping[str, str]
    #: Set when the sandbox backend was not a real isolation boundary.
    isolation_warning: str | None = None

    @property
    def request_id(self) -> str:
        return self.request.request_id


class Reviewer(ABC):
    """Turns a presentation into an approve/reject decision."""

    #: Recorded in the audit log as the deciding actor.
    name: str = "reviewer"

    @abstractmethod
    def review(self, presentation: ReviewPresentation) -> Decision:
        """Approve or reject. Implementations may block on human input."""

    def opening(self, total: int) -> None:
        """Called once before the first review. Optional."""

    def closing(self, decisions: list[Decision]) -> None:
        """Called once after the last review. Optional."""
