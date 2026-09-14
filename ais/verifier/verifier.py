"""The Verifier: run the rules over a sandbox observation and recommend a verdict.

The Verifier does not decide anything. It produces the evidence and a
recommendation; a human makes the call. That separation is the whole point --
an automated gate that silently rejects is just a different single point of
failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ais.config import Settings
from ais.models import Anomaly, EditRequest, ExecutionReport, SandboxResult, Severity, Verdict
from ais.verifier.rules import RULES, Rule


@dataclass(frozen=True)
class VerificationContext:
    """Everything a rule is allowed to see.

    Built from an :class:`~ais.models.EditRequest` with its ``expected`` label
    deliberately dropped. The evaluation harness knows which scenarios are
    planted; the Verifier must not, or the detection numbers would measure
    nothing but its ability to read an answer key.
    """

    request_id: str
    targets: tuple[str, ...]
    diff: str
    proposed: Mapping[str, str]
    result: SandboxResult
    settings: Settings

    @classmethod
    def build(
        cls, request: EditRequest, diff: str, result: SandboxResult, settings: Settings
    ) -> "VerificationContext":
        return cls(
            request_id=request.request_id,
            targets=tuple(request.targets),
            diff=diff,
            proposed=dict(request.proposed),
            result=result,
            settings=settings,
        )


class Verifier:
    """Applies the rule set to one sandboxed run."""

    def __init__(self, settings: Settings, rules: list[Rule] | None = None) -> None:
        self.settings = settings
        self.rules = list(rules if rules is not None else RULES)

    def evaluate(
        self, request: EditRequest, diff: str, result: SandboxResult
    ) -> ExecutionReport:
        context = VerificationContext.build(request, diff, result, self.settings)

        anomalies: list[Anomaly] = []
        for rule in self.rules:
            try:
                anomalies.extend(rule.evaluate(context))
            except Exception as exc:  # noqa: BLE001
                # A rule that throws must not take the verdict down with it, and
                # must not silently vanish either: the reviewer is told that one
                # of the checks did not run.
                anomalies.append(
                    Anomaly(
                        rule_id="verifier.rule_error",
                        severity=Severity.MEDIUM,
                        title=f"Rule {rule.id!r} failed to evaluate",
                        detail=f"{type(exc).__name__}: {exc}. This check did not run.",
                        advisory=True,
                    )
                )

        anomalies.sort(key=lambda a: (-a.severity.rank, a.advisory, a.rule_id))
        return ExecutionReport(
            request_id=request.request_id,
            verdict=decide(anomalies),
            anomalies=tuple(anomalies),
            sandbox=result,
        )


def decide(anomalies: list[Anomaly]) -> Verdict:
    """Map findings to a recommendation.

    Advisory anomalies -- "this backend is not isolated", "the trace is
    truncated" -- describe the run rather than the edit and are excluded. If
    they counted, every edit evaluated in a degraded environment would register
    as a detection, and the evaluation's false-positive rate would be noise.
    """
    findings = [a for a in anomalies if not a.advisory]
    if not findings:
        return Verdict.PASS
    worst = max(a.severity.rank for a in findings)
    if worst >= Severity.HIGH.rank:
        return Verdict.BLOCK
    if worst >= Severity.MEDIUM.rank:
        return Verdict.FLAG
    return Verdict.PASS
