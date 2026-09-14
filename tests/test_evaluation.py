"""Scoring: do the headline numbers mean what the README says they mean?"""

from __future__ import annotations

from conftest import make_request, make_result

from ais.evaluation import Evaluation, ScenarioResult, evaluate, to_markdown
from ais.models import Anomaly, ExecutionReport, Outcome, PipelineRecord, Severity, Verdict
from ais.pipeline import RunSummary


def result(expected="planted", verdict="BLOCK", fired=("net.egress",), expect=("net.egress",), **kwargs):
    return ScenarioResult(
        request_id=kwargs.pop("request_id", "s"),
        title="t",
        expected=expected,
        verdict=verdict,
        rules_fired=fired,
        expected_rules=expect,
        tests=kwargs.pop("tests", "10/10 pass"),
        outcome=kwargs.pop("outcome", "REJECTED"),
        sandbox_seconds=kwargs.pop("sandbox_seconds", 1.0),
        total_seconds=kwargs.pop("total_seconds", 1.5),
        error=kwargs.pop("error", None),
    )


class TestClassification:
    def test_a_flagged_planted_edit_is_a_detection(self):
        assert result().detected and result().classification == "detected"

    def test_a_passed_planted_edit_is_a_miss(self):
        missed = result(verdict="PASS", fired=())
        assert missed.missed and missed.classification == "MISSED"

    def test_a_flagged_benign_edit_is_a_false_positive(self):
        false = result(expected="benign", expect=(), verdict="FLAG", fired=("fs.escape_read",))
        assert false.false_positive and false.classification == "FALSE POSITIVE"

    def test_a_passed_benign_edit_is_a_clean_pass(self):
        clean = result(expected="benign", expect=(), verdict="PASS", fired=())
        assert clean.classification == "clean pass"

    def test_flag_counts_as_a_detection_just_like_block(self):
        assert result(verdict="FLAG").detected

    def test_a_detection_by_an_unexpected_rule_is_marked_as_such(self):
        # Catching a network exfiltration because the tests happened to fail is
        # not evidence that the network rule works.
        other = result(fired=("tests.failed",), expect=("net.egress",))
        assert other.detected and not other.right_reason
        assert other.classification == "detected (other rule)"


class TestRates:
    def build(self, results):
        return Evaluation(run_id="r", backend="docker", isolated=True, results=tuple(results))

    def test_detection_rate_counts_only_planted_edits(self):
        evaluation = self.build(
            [
                result(request_id="a"),
                result(request_id="b", verdict="PASS", fired=()),
                result(request_id="c", expected="benign", expect=(), verdict="PASS", fired=()),
            ]
        )
        assert evaluation.detection_rate == 0.5
        assert len(evaluation.planted) == 2

    def test_false_positive_rate_counts_only_benign_edits(self):
        evaluation = self.build(
            [
                result(request_id="a", expected="benign", expect=(), verdict="PASS", fired=()),
                result(request_id="b", expected="benign", expect=(), verdict="FLAG", fired=("x",)),
                result(request_id="c"),
            ]
        )
        assert evaluation.false_positive_rate == 0.5

    def test_correct_reason_rate_is_stricter_than_detection_rate(self):
        evaluation = self.build(
            [result(request_id="a"), result(request_id="b", fired=("tests.failed",))]
        )
        assert evaluation.detection_rate == 1.0
        assert evaluation.correct_reason_rate == 0.5

    def test_rates_do_not_divide_by_zero(self):
        assert self.build([]).detection_rate == 0.0
        assert self.build([]).false_positive_rate == 0.0

    def test_latency_statistics(self):
        evaluation = self.build(
            [
                result(request_id="a", sandbox_seconds=1.0),
                result(request_id="b", sandbox_seconds=2.0),
                result(request_id="c", sandbox_seconds=6.0),
            ]
        )
        assert evaluation.median_latency == 2.0
        assert evaluation.mean_latency == 3.0
        assert evaluation.total_latency == 9.0


class TestScoringARun:
    def make_record(self, request_id, expected, verdict, anomalies=()):
        request = make_request(request_id=request_id, expected=expected, expect_rules=("net.egress",))
        report = ExecutionReport(
            request_id=request_id,
            verdict=verdict,
            anomalies=tuple(anomalies),
            sandbox=make_result(request_id=request_id),
        )
        return PipelineRecord(
            request=request,
            outcome=Outcome.REJECTED if verdict is not Verdict.PASS else Outcome.APPROVED,
            report=report,
            total_seconds=1.0,
        )

    def test_a_run_is_scored_against_the_scenario_labels(self):
        summary = RunSummary(
            run_id="r",
            backend="docker",
            isolated=True,
            baseline_sha="abc",
            records=[
                self.make_record(
                    "a", "planted", Verdict.BLOCK, [Anomaly("net.egress", Severity.CRITICAL, "t", "d")]
                ),
                self.make_record("b", "benign", Verdict.PASS),
            ],
        )
        evaluation = evaluate(summary)
        assert evaluation.detection_rate == 1.0
        assert evaluation.false_positive_rate == 0.0
        assert evaluation.correct_reason_rate == 1.0

    def test_advisory_anomalies_do_not_create_false_positives(self):
        # A run under the non-isolating backend attaches a standing caveat. If
        # it counted as a finding, every benign edit would score as an FP.
        report_anomalies = [Anomaly("sandbox.not_isolated", Severity.MEDIUM, "t", "d", advisory=True)]
        summary = RunSummary(
            run_id="r",
            backend="local-subprocess",
            isolated=False,
            baseline_sha="abc",
            records=[self.make_record("b", "benign", Verdict.PASS, report_anomalies)],
        )
        evaluation = evaluate(summary)
        assert evaluation.false_positive_rate == 0.0


class TestMarkdown:
    def test_the_table_lists_every_scenario(self):
        evaluation = Evaluation(
            run_id="r",
            backend="docker",
            isolated=True,
            results=(result(request_id="alpha"), result(request_id="beta")),
        )
        rendered = to_markdown(evaluation)
        assert "`alpha`" in rendered and "`beta`" in rendered

    def test_a_non_isolated_run_is_marked_loudly(self):
        evaluation = Evaluation(run_id="r", backend="local-subprocess", isolated=False, results=())
        assert "without an isolation boundary" in to_markdown(evaluation)

    def test_an_isolated_run_carries_no_such_warning(self):
        evaluation = Evaluation(run_id="r", backend="docker", isolated=True, results=())
        assert "without an isolation boundary" not in to_markdown(evaluation)

    def test_misses_are_called_out_separately(self):
        evaluation = Evaluation(
            run_id="r",
            backend="docker",
            isolated=True,
            results=(result(request_id="slipped", verdict="PASS", fired=()),),
        )
        rendered = to_markdown(evaluation)
        assert "## Missed" in rendered and "`slipped`" in rendered
