"""The scenario set itself: is the evaluation input actually valid?

A scenario that produces an empty diff, or names a rule that does not exist,
would quietly score as a pass and inflate the results. These tests check the
evaluation's inputs before the evaluation is allowed to mean anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ais import patchkit
from ais.editor import EditorError, ScriptedEditor
from ais.verifier.rules import RULES

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "scenarios" / "scenarios.yaml"
TEMPLATE = ROOT / "sample_project"


@pytest.fixture(scope="module")
def requests():
    return ScriptedEditor(SCENARIOS).requests()


def ids(requests):
    return [request.request_id for request in requests]


class TestLoading:
    def test_the_scenario_file_loads(self, requests):
        assert len(requests) == 10

    def test_ids_are_unique(self, requests):
        assert len(set(ids(requests))) == len(requests)

    def test_the_set_contains_both_benign_and_planted_edits(self, requests):
        labels = {request.expected for request in requests}
        assert labels == {"benign", "planted"}

    def test_there_are_enough_benign_edits_to_measure_false_positives(self, requests):
        assert sum(1 for r in requests if r.expected == "benign") >= 3

    def test_selecting_by_substring_works(self, requests):
        editor = ScriptedEditor(SCENARIOS)
        assert ids(editor.select(["plant-06"])) == ["plant-06-net-exfil"]

    def test_selecting_an_unknown_id_is_an_error(self):
        with pytest.raises(EditorError, match="no scenario matches"):
            ScriptedEditor(SCENARIOS).select(["does-not-exist"])


class TestPayloadIntegrity:
    def test_every_target_has_payload_content(self, requests):
        for request in requests:
            assert set(request.targets) == set(request.proposed)

    def test_every_scenario_produces_a_non_empty_diff(self, requests):
        # An empty diff means nothing is verified and the scenario scores a free
        # pass. Regenerate with `python scenarios/build_payloads.py`.
        for request in requests:
            changes = {}
            for target in request.targets:
                original = TEMPLATE / target
                current = patchkit.read_text_exact(str(original)) if original.is_file() else None
                changes[target] = (current, request.proposed[target])
            assert patchkit.make_patch(changes).strip(), f"{request.request_id} has an empty diff"

    def test_proposed_content_parses_or_is_meant_not_to(self, requests):
        import ast

        for request in requests:
            for target, content in request.proposed.items():
                if not target.endswith(".py"):
                    continue
                ast.parse(content)  # every planted edit here is valid Python


class TestGroundTruth:
    def test_planted_scenarios_declare_the_rule_they_exercise(self, requests):
        for request in requests:
            if request.expected == "planted":
                assert request.expect_rules, f"{request.request_id} declares no expected rule"

    def test_benign_scenarios_expect_no_rules(self, requests):
        for request in requests:
            if request.expected == "benign":
                assert request.expect_rules == ()

    def test_expected_rules_all_exist(self, requests):
        known = {rule.id for rule in RULES}
        for request in requests:
            unknown = set(request.expect_rules) - known
            assert not unknown, f"{request.request_id} names unknown rule(s): {sorted(unknown)}"

    def test_the_planted_set_covers_a_spread_of_detections(self, requests):
        # If every planted scenario tripped the same rule the headline number
        # would say almost nothing about the rule set as a whole.
        exercised = {rule for request in requests for rule in request.expect_rules}
        assert len(exercised) >= 6, f"only {len(exercised)} distinct rules exercised"

    def test_requests_carry_no_absolute_paths(self, requests):
        # The editor never holds a real filesystem path. This is the property
        # the whole mediation design rests on.
        for request in requests:
            for target in request.targets:
                assert not target.startswith("/")
                assert ".." not in target
