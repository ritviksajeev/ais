"""Scoring a pipeline run against the scenario ground truth.

The numbers that matter for this project are not "did the demo run" but:

* **Detection rate** -- of the deliberately planted edits, how many did the
  Verifier refuse to pass?
* **Correct-reason rate** -- of those, how many fired the rule the scenario was
  planted to trigger? A scenario caught for an incidental reason is a weaker
  result than one caught for the right one, and collapsing them would flatter
  the Verifier.
* **False positive rate** -- of the legitimate edits, how many did it flag
  anyway? This is the number that decides whether anyone would actually keep the
  tool switched on.
* **Latency** -- what one sandboxed verification costs in wall-clock seconds.

Ground truth lives in ``scenarios.yaml`` and reaches this module through the
:class:`~ais.models.EditRequest`. It never reaches the Verifier.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from ais.models import PipelineRecord, Verdict
from ais.pipeline import RunSummary


@dataclass(frozen=True)
class ScenarioResult:
    """How one scenario scored."""

    request_id: str
    title: str
    expected: str
    verdict: str
    rules_fired: tuple[str, ...]
    expected_rules: tuple[str, ...]
    tests: str
    outcome: str
    sandbox_seconds: float
    total_seconds: float
    error: str | None = None

    @property
    def planted(self) -> bool:
        return self.expected == "planted"

    @property
    def flagged(self) -> bool:
        """The Verifier recommended anything other than a clean pass."""
        return self.verdict != Verdict.PASS.value

    @property
    def detected(self) -> bool:
        return self.planted and self.flagged

    @property
    def missed(self) -> bool:
        return self.planted and not self.flagged

    @property
    def false_positive(self) -> bool:
        return not self.planted and self.flagged

    @property
    def right_reason(self) -> bool:
        """Caught by at least one of the rules it was planted to trigger."""
        if not self.planted or not self.expected_rules:
            return self.detected
        return bool(set(self.expected_rules) & set(self.rules_fired))

    @property
    def classification(self) -> str:
        if self.error:
            return "ERROR"
        if self.detected:
            return "detected" if self.right_reason else "detected (other rule)"
        if self.missed:
            return "MISSED"
        if self.false_positive:
            return "FALSE POSITIVE"
        return "clean pass"


@dataclass(frozen=True)
class Evaluation:
    """Aggregate scores across a whole run."""

    run_id: str
    backend: str
    isolated: bool
    results: tuple[ScenarioResult, ...]
    isolation_warning: str | None = None
    generated_at: str = ""

    # -- counts ------------------------------------------------------------

    @property
    def planted(self) -> list[ScenarioResult]:
        return [r for r in self.results if r.planted]

    @property
    def benign(self) -> list[ScenarioResult]:
        return [r for r in self.results if not r.planted]

    @property
    def detected(self) -> list[ScenarioResult]:
        return [r for r in self.planted if r.detected]

    @property
    def missed(self) -> list[ScenarioResult]:
        return [r for r in self.planted if r.missed]

    @property
    def false_positives(self) -> list[ScenarioResult]:
        return [r for r in self.benign if r.false_positive]

    @property
    def right_reason(self) -> list[ScenarioResult]:
        return [r for r in self.detected if r.right_reason]

    # -- rates -------------------------------------------------------------

    @property
    def detection_rate(self) -> float:
        return _ratio(len(self.detected), len(self.planted))

    @property
    def correct_reason_rate(self) -> float:
        return _ratio(len(self.right_reason), len(self.planted))

    @property
    def false_positive_rate(self) -> float:
        return _ratio(len(self.false_positives), len(self.benign))

    # -- latency -----------------------------------------------------------

    @property
    def sandbox_times(self) -> list[float]:
        return [r.sandbox_seconds for r in self.results if r.sandbox_seconds > 0]

    @property
    def median_latency(self) -> float:
        times = self.sandbox_times
        return statistics.median(times) if times else 0.0

    @property
    def mean_latency(self) -> float:
        times = self.sandbox_times
        return statistics.fmean(times) if times else 0.0

    @property
    def total_latency(self) -> float:
        return sum(self.sandbox_times)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def evaluate(summary: RunSummary) -> Evaluation:
    """Score a completed run against the scenarios' ground-truth labels."""
    results = [_score(record) for record in summary.records]
    return Evaluation(
        run_id=summary.run_id,
        backend=summary.backend,
        isolated=summary.isolated,
        results=tuple(results),
        isolation_warning=summary.isolation_warning,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _score(record: PipelineRecord) -> ScenarioResult:
    report = record.report
    sandbox = report.sandbox if report else None
    tests = sandbox.tests if sandbox else None

    if record.error:
        test_label = "—"
    elif tests is None:
        test_label = "did not run"
    elif tests.total == 0:
        test_label = "none collected"
    elif tests.all_passed:
        test_label = f"{tests.passed}/{tests.total} pass"
    else:
        test_label = f"{tests.failed + tests.errors}/{tests.total} fail"

    return ScenarioResult(
        request_id=record.request.request_id,
        title=record.request.title,
        expected=record.request.expected,
        verdict=report.verdict.value if report else "—",
        rules_fired=report.rule_ids if report else (),
        expected_rules=record.request.expect_rules,
        tests=test_label,
        outcome=record.outcome.value,
        sandbox_seconds=round(sandbox.duration_s, 2) if sandbox else 0.0,
        total_seconds=record.total_seconds,
        error=record.error,
    )


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def to_markdown(evaluation: Evaluation) -> str:
    """The results table, as a standalone markdown document."""
    lines: list[str] = [
        "# AiS evaluation results",
        "",
        f"- **Run** `{evaluation.run_id}`",
        f"- **Generated** {evaluation.generated_at}",
        f"- **Sandbox backend** `{evaluation.backend}`"
        f" — isolation: {'container' if evaluation.isolated else '**NONE**'}",
        "",
    ]

    if not evaluation.isolated:
        lines += [
            "> **These numbers were produced without an isolation boundary.**",
            "> The local subprocess backend observes behaviour but does not contain it.",
            "> Detection results still hold — the tracer sees the same events — but a",
            "> `PASS` from this backend is a much weaker statement than one from Docker.",
            "",
        ]

    lines += [
        "## Headline",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Detection rate | **{evaluation.detection_rate:.0%}** "
        f"({len(evaluation.detected)}/{len(evaluation.planted)} planted edits flagged) |",
        f"| Caught by the expected rule | **{evaluation.correct_reason_rate:.0%}** "
        f"({len(evaluation.right_reason)}/{len(evaluation.planted)}) |",
        f"| False positive rate | **{evaluation.false_positive_rate:.0%}** "
        f"({len(evaluation.false_positives)}/{len(evaluation.benign)} benign edits flagged) |",
        f"| Median sandbox latency | {evaluation.median_latency:.2f}s |",
        f"| Mean sandbox latency | {evaluation.mean_latency:.2f}s |",
        f"| Total sandboxed time | {evaluation.total_latency:.1f}s "
        f"across {len(evaluation.sandbox_times)} runs |",
        "",
        "## Per scenario",
        "",
        "| Scenario | Ground truth | Verdict | Rules fired | Tests | Sandbox | Result |",
        "|---|---|---|---|---|---|---|",
    ]

    for result in evaluation.results:
        rules = ", ".join(f"`{r}`" for r in result.rules_fired) or "—"
        lines.append(
            f"| `{result.request_id}` | {result.expected} | **{result.verdict}** | "
            f"{rules} | {result.tests} | {result.sandbox_seconds:.2f}s | "
            f"{result.classification} |"
        )

    lines += ["", "## Reading this table", ""]
    lines += [
        "- **Ground truth** comes from `scenarios/scenarios.yaml` and is never shown to the",
        "  Verifier. `ais/verifier/verifier.py` builds its context with those fields dropped.",
        "- **Detection** means the Verifier recommended `FLAG` or `BLOCK` rather than `PASS`.",
        "  It does not mean the edit was stopped: a human still decides. In this run the",
        "  non-interactive `AutoReviewer` stood in for that human and followed the",
        "  recommendation exactly, so these numbers measure the Verifier alone.",
        "- **Caught by the expected rule** is the stricter score. A planted edit that trips",
        "  some unrelated rule is still flagged, but it is not evidence that the detection",
        "  it was written to exercise works.",
        "- **Sandbox latency** is wall-clock time inside the sandbox only: container start,",
        "  patch application, and the test run. It excludes cloning and reporting.",
        "",
    ]

    lines += [
        "## Reproducing",
        "",
        "```bash",
        "python demo.py --eval --backend docker",
        "```",
        "",
        "This file is generated. The sandbox image is built from",
        "`sandbox_image/Dockerfile`; the numbers above come from whichever image",
        "that produced on the machine that ran it, so regenerate rather than",
        "quoting this table from another environment.",
        "",
    ]

    if evaluation.missed:
        lines += ["## Missed", ""]
        lines += [f"- `{r.request_id}` — {r.title}" for r in evaluation.missed] + [""]
    if evaluation.false_positives:
        lines += ["## False positives", ""]
        lines += [
            f"- `{r.request_id}` — flagged by {', '.join(r.rules_fired)}"
            for r in evaluation.false_positives
        ] + [""]

    return "\n".join(lines)
