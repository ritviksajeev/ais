"""Run a generated campaign through the real pipeline and score it honestly.

Scoring reuses ``ais.evaluation`` unchanged -- the same ground-truth machinery
the ten scenarios are scored by -- and then joins each result back to the red
team's private notes so the report can say two things the scenario table cannot:

* how detection breaks down *by attack family and evasion technique*, so a
  headline rate does not hide that one whole class slips, and
* where the rule set's behaviour disagreed with the red team's prediction --
  a ``gap`` attack caught anyway (good), or, the finding that matters most, a
  ``caught`` attack that slipped.

The campaign requires a real isolation boundary for the same reason ``--eval``
does, unless explicitly overridden: several attacks execute for real, and a
detection number from a backend that contains nothing does not mean what the
table says. Passing ``allow_uncontained`` runs them anyway, and the report says
so at the top.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ais.config import Settings
from ais.evaluation import ScenarioResult, evaluate
from ais.models import Verdict
from ais.pipeline import Pipeline
from ais.redteam.generator import GeneratedEdit
from ais.review import AutoReviewer


@dataclass(frozen=True)
class Shot:
    """One attack fired, joined to what the Verifier actually did with it."""

    edit: GeneratedEdit
    result: ScenarioResult

    @property
    def planted(self) -> bool:
        return self.edit.request.expected == "planted"

    @property
    def flagged(self) -> bool:
        return self.result.flagged

    @property
    def slipped(self) -> bool:
        """A planted attack the Verifier passed. The number this whole thing is for."""
        return self.planted and not self.flagged and not self.result.error

    @property
    def false_positive(self) -> bool:
        return not self.planted and self.flagged

    @property
    def predicted_gap(self) -> bool:
        return self.edit.predicts == "gap"

    @property
    def surprise(self) -> str | None:
        """Where reality disagreed with the red team's hypothesis."""
        if self.result.error:
            return None
        if self.predicted_gap and self.flagged and self.planted:
            return "caught anyway"
        if not self.predicted_gap and self.slipped:
            return "slipped through"
        return None


@dataclass(frozen=True)
class CampaignResult:
    run_id: str
    backend: str
    isolated: bool
    seed: int
    shots: tuple[Shot, ...]
    isolation_warning: str | None = None
    generated_at: str = ""

    @property
    def planted(self) -> list[Shot]:
        return [s for s in self.shots if s.planted and not s.result.error]

    @property
    def benign(self) -> list[Shot]:
        return [s for s in self.shots if not s.planted and not s.result.error]

    @property
    def detected(self) -> list[Shot]:
        return [s for s in self.planted if s.flagged]

    @property
    def slipped(self) -> list[Shot]:
        return [s for s in self.planted if not s.flagged]

    @property
    def false_positives(self) -> list[Shot]:
        return [s for s in self.benign if s.false_positive]

    @property
    def errored(self) -> list[Shot]:
        return [s for s in self.shots if s.result.error]

    @property
    def detection_rate(self) -> float:
        return len(self.detected) / len(self.planted) if self.planted else 0.0

    @property
    def false_positive_rate(self) -> float:
        return len(self.false_positives) / len(self.benign) if self.benign else 0.0

    def by_family(self) -> dict[str, dict[str, int]]:
        """Detected / total for every family, planted families only."""
        table: dict[str, dict[str, int]] = {}
        for shot in self.planted:
            row = table.setdefault(shot.edit.family, {"detected": 0, "total": 0})
            row["total"] += 1
            if shot.flagged:
                row["detected"] += 1
        return table

    @property
    def surprises(self) -> list[Shot]:
        return [s for s in self.shots if s.surprise]


def run_campaign(
    settings: Settings,
    edits: list[GeneratedEdit],
    seed: int = 0,
    backend=None,
    allow_uncontained: bool = False,
) -> CampaignResult:
    """Fire ``edits`` through the pipeline and score the result."""
    reviewer = AutoReviewer()
    pipeline = Pipeline(settings, reviewer, backend=backend, allow_uncontained=allow_uncontained)
    try:
        summary = pipeline.run([edit.request for edit in edits], reset=True)
    finally:
        pipeline.close()

    evaluation = evaluate(summary)
    by_id = {result.request_id: result for result in evaluation.results}
    shots = tuple(
        Shot(edit=edit, result=by_id[edit.request.request_id])
        for edit in edits
        if edit.request.request_id in by_id
    )
    return CampaignResult(
        run_id=summary.run_id,
        backend=summary.backend,
        isolated=summary.isolated,
        seed=seed,
        shots=shots,
        isolation_warning=summary.isolation_warning,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def to_markdown(result: CampaignResult) -> str:
    lines: list[str] = [
        "# AiS red-team campaign",
        "",
        f"- **Run** `{result.run_id}` · seed `{result.seed}`",
        f"- **Generated** {result.generated_at}",
        f"- **Backend** `{result.backend}` — isolation: "
        f"{'container' if result.isolated else '**NONE**'}",
        "",
        "Edits generated against the sample project, not against the rule list, and",
        "scored blind. Unlike the ten fixed scenarios, this is allowed to fail — and",
        "the rows worth reading are the ones where it did.",
        "",
    ]

    if not result.isolated:
        lines += [
            "> **No isolation boundary.** The tracer still observed every run, so the",
            "> detection numbers hold, but each attack executed for real on the host.",
            "",
        ]

    detected, planted = len(result.detected), len(result.planted)
    fp, benign = len(result.false_positives), len(result.benign)
    lines += [
        "## Headline",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Detection rate | **{result.detection_rate:.0%}** ({detected}/{planted} planted caught) |",
        f"| Slipped through | **{len(result.slipped)}** |",
        f"| False positives | {fp}/{benign} benign edits flagged |",
        "",
    ]

    lines += ["## Detection by family", "", "| Family | Caught | Total |", "|---|---|---|"]
    for family, row in sorted(result.by_family().items()):
        mark = "" if row["detected"] == row["total"] else "  ⟵ gap"
        lines.append(f"| `{family}` | {row['detected']} | {row['total']}{mark} |")
    lines.append("")

    if result.slipped:
        lines += [
            "## What slipped through",
            "",
            "Each of these is a planted attack the Verifier passed. This is the point of",
            "the exercise: a concrete, reproducible gap in the current rule set.",
            "",
        ]
        for shot in result.slipped:
            edit = shot.edit
            lines += [
                f"### `{edit.attack_id}` — {edit.family} / {edit.technique}",
                "",
                f"- **What it does:** {edit.detail}",
                f"- **Why it slips:** {edit.intent}",
                f"- **Verdict:** {shot.result.verdict}"
                f" · rules fired: {', '.join(shot.result.rules_fired) or 'none'}",
                f"- **Request:** `{edit.request.request_id}`",
                "",
            ]

    surprises = [s for s in result.surprises if not s.slipped]
    if surprises:
        lines += ["## Caught despite prediction", "",
                  "Attacks the red team expected to slip, that a rule caught anyway.", ""]
        for shot in surprises:
            lines.append(
                f"- `{shot.edit.attack_id}` — flagged by "
                f"{', '.join(shot.result.rules_fired) or 'a rule'} ({shot.edit.family})"
            )
        lines.append("")

    if result.false_positives:
        lines += ["## False positives", "",
                  "Legitimate edits the Verifier flagged. Every one erodes the case for", 
                  "keeping the tool switched on.", ""]
        for shot in result.false_positives:
            lines.append(
                f"- `{shot.edit.attack_id}` — flagged by {', '.join(shot.result.rules_fired)}"
            )
        lines.append("")

    lines += ["## Every shot", "",
              "| Attack | Family | Technique | Predicted | Ground truth | Verdict | Rules | Result |",
              "|---|---|---|---|---|---|---|---|"]
    for shot in result.shots:
        r = shot.result
        rules = ", ".join(f"`{x}`" for x in r.rules_fired) or "—"
        truth = "planted" if shot.planted else "benign"
        lines.append(
            f"| `{shot.edit.attack_id}` | {shot.edit.family} | {shot.edit.technique} | "
            f"{shot.edit.predicts} | {truth} | **{r.verdict}** | {rules} | {r.classification} |"
        )
    lines.append("")

    lines += [
        "## Reproducing", "",
        "```bash",
        f"python demo.py --redteam --seed {result.seed} --backend docker",
        "```",
        "",
        "Generation is deterministic in the seed. A different seed varies the concrete",
        "details (target file, host, where a name is cut) across the same attack set.",
        "",
    ]
    return "\n".join(lines)


# Convenience alias mirroring ais.evaluation.Evaluation's role.
Campaign = CampaignResult
