"""A non-interactive reviewer, for batch runs and the evaluation harness.

This is a *stand-in* for a human, not a replacement for one. It exists because
the evaluation needs to push ten scenarios through the pipeline without someone
sitting at a prompt, and because a demo should be runnable in CI.

It follows the Verifier's recommendation exactly, which makes the evaluation a
measurement of the Verifier alone -- approving everything it passes and
rejecting everything it flags. That is precisely the behaviour AiS argues
against in production: the whole design puts a human at this step because an
automatic gate is just a different single point of failure. Using it here is a
measurement convenience, and the numbers it produces are the Verifier's, not a
claim that the human can be removed.
"""

from __future__ import annotations

from ais.models import Decision, Verdict
from ais.review.base import Reviewer, ReviewPresentation


class AutoReviewer(Reviewer):
    """Follows the Verifier's recommendation without a human in the loop."""

    name = "reviewer:auto"

    def __init__(self, approve_on: tuple[Verdict, ...] = (Verdict.PASS,)) -> None:
        self.approve_on = approve_on

    def review(self, presentation: ReviewPresentation) -> Decision:
        report = presentation.report
        approved = report.verdict in self.approve_on
        if approved:
            reason = "verifier reported no findings; auto-approved in non-interactive mode"
        else:
            rules = ", ".join(report.rule_ids) or "unspecified"
            reason = f"verifier recommended {report.verdict.value} ({rules})"
        return Decision(
            request_id=presentation.request_id,
            approved=approved,
            reviewer=self.name,
            reason=reason,
        )
