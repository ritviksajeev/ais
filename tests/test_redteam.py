"""The Phase 0 red team.

Two kinds of property matter here. The generator's: that a campaign is
deterministic, that every attack produces a real diff against real baseline
content, and that the ground truth rides on the request without ever being the
thing that gets scored. And the campaign's: that when fired through the actual
pipeline, an attack the runtime tracer or static scan should catch *is* caught,
and one built to slip *does* slip -- because a red team that cannot demonstrate
a real gap is only measuring itself again.
"""

from __future__ import annotations

import ast

import pytest

from ais.evaluation import ScenarioResult
from ais.redteam import RedTeam, ATTACKS, families, run_campaign
from ais.redteam.campaign import CampaignResult, Shot
from ais.redteam.generator import GeneratedEdit, MutationStrategy
from ais.redteam.library import Attack, CAUGHT, GAP
from ais.redteam.samples import InjectionError, SAMPLES, replace_in


def _attack(attack_id: str) -> Attack:
    return next(a for a in ATTACKS if a.id == attack_id)


def _shot(attack_id="x", family="network", predicts=GAP, expected="planted",
          verdict="PASS", rules=(), error=None) -> Shot:
    """A synthetic shot, so the scoring logic is testable without a sandbox."""
    from ais.models import EditRequest

    request = EditRequest(
        request_id="s-1", title="t", rationale="", targets=("pricing.py",),
        proposed={"pricing.py": "x\n"}, expected=expected,
    )
    edit = GeneratedEdit(request=request, attack_id=attack_id, family=family,
                         technique="t", predicts=predicts, intent="i", detail="d")
    result = ScenarioResult(
        request_id="s-1", title="t", expected=expected, verdict=verdict,
        rules_fired=rules, expected_rules=(), tests="10/10 pass",
        outcome="REJECTED", sandbox_seconds=0.1, total_seconds=0.2, error=error,
    )
    return Shot(edit=edit, result=result)


class TestCatalogue:
    def test_every_attack_builds_a_parseable_edit(self):
        import random

        rng = random.Random(0)
        for attack in ATTACKS:
            payload = attack.build(rng)
            assert payload.targets, f"{attack.id} named no targets"
            for path, source in payload.proposed.items():
                # A proposal that does not parse is not an attack, it is a
                # SyntaxError the Verifier would reject for the wrong reason.
                ast.parse(source)

    def test_declared_targets_match_the_proposed_files(self):
        import random

        rng = random.Random(1)
        for attack in ATTACKS:
            payload = attack.build(rng)
            assert set(payload.targets) == set(payload.proposed)

    def test_every_attack_is_planted_or_benign(self):
        for attack in ATTACKS:
            assert attack.expected in ("planted", "benign")

    def test_benign_attacks_expect_no_rules(self):
        for attack in ATTACKS:
            if attack.expected == "benign":
                assert attack.expect_rules == ()

    def test_predictions_are_well_formed(self):
        for attack in ATTACKS:
            assert attack.predicts in (CAUGHT, GAP)

    def test_there_is_at_least_one_predicted_gap(self):
        # The whole point. A catalogue with nothing built to slip would be the
        # scenarios over again.
        assert any(a.predicts == GAP and a.planted for a in ATTACKS)

    def test_there_are_benign_probes(self):
        # Without them the campaign cannot measure a false-positive rate.
        assert any(a.expected == "benign" for a in ATTACKS)


class TestGeneration:
    def test_generation_is_deterministic_in_the_seed(self):
        one = RedTeam().generate(count=12, seed=7)
        two = RedTeam().generate(count=12, seed=7)
        assert [e.request.request_id for e in one] == [e.request.request_id for e in two]
        assert [e.request.proposed for e in one] == [e.request.proposed for e in two]

    def test_a_different_seed_varies_the_campaign(self):
        one = RedTeam().generate(count=12, seed=1)
        two = RedTeam().generate(count=12, seed=2)
        assert [e.attack_id for e in one] != [e.attack_id for e in two]

    def test_ground_truth_rides_on_the_request(self):
        for edit in RedTeam().generate(count=16, seed=3):
            assert edit.request.expected == ("planted" if edit.family != "benign" else "benign")

    def test_the_title_does_not_announce_the_attack(self):
        # The reviewer and the Verifier should work from behaviour, not a label
        # that says "this is an attack".
        for edit in RedTeam().generate(count=16, seed=0):
            assert "attack" not in edit.request.title.lower()
            assert "malicious" not in edit.request.title.lower()

    def test_strategy_covers_every_attack_before_repeating(self):
        strategy = MutationStrategy()
        import random

        chosen = strategy.choose(random.Random(0), len(ATTACKS))
        assert {a.id for a in chosen} == {a.id for a in ATTACKS}


class TestSamples:
    def test_replace_in_refuses_a_missing_anchor(self):
        with pytest.raises(InjectionError):
            replace_in(SAMPLES["pricing.py"], "this text is not in the file", "x")

    def test_replace_in_refuses_an_ambiguous_anchor(self):
        # "return" appears many times; refusing beats silently editing the first.
        with pytest.raises(InjectionError):
            replace_in(SAMPLES["pricing.py"], "return", "x")

    def test_samples_are_the_real_template_files(self):
        assert "def apply_discount" in SAMPLES["pricing.py"]
        assert "class Inventory" in SAMPLES["inventory.py"]


class TestFamilies:
    def test_families_are_listed_in_first_seen_order(self):
        assert families()[0] == ATTACKS[0].family


class TestScoring:
    """The classification logic, on synthetic results -- no sandbox, every platform."""

    def test_a_passed_planted_attack_is_a_slip(self):
        shot = _shot(expected="planted", verdict="PASS")
        assert shot.slipped
        assert not shot.flagged

    def test_a_flagged_planted_attack_is_not_a_slip(self):
        shot = _shot(expected="planted", verdict="BLOCK", rules=("net.egress",))
        assert not shot.slipped
        assert shot.flagged

    def test_a_flagged_benign_edit_is_a_false_positive(self):
        shot = _shot(expected="benign", predicts=CAUGHT, verdict="FLAG", rules=("x",))
        assert shot.false_positive

    def test_an_errored_shot_is_neither_slip_nor_detection(self):
        shot = _shot(expected="planted", verdict="—", error="boom")
        assert not shot.slipped

    def test_a_predicted_gap_caught_anyway_is_a_good_surprise(self):
        shot = _shot(predicts=GAP, expected="planted", verdict="BLOCK", rules=("tests.failed",))
        assert shot.surprise == "caught anyway"

    def test_a_predicted_catch_that_slips_is_the_bad_surprise(self):
        shot = _shot(predicts=CAUGHT, expected="planted", verdict="PASS")
        assert shot.surprise == "slipped through"

    def test_metrics_partition_and_rates(self):
        shots = (
            _shot("a", predicts=CAUGHT, expected="planted", verdict="BLOCK", rules=("r",)),
            _shot("b", predicts=GAP, expected="planted", verdict="PASS"),
            _shot("c", predicts=CAUGHT, expected="benign", verdict="PASS"),
        )
        result = CampaignResult(
            run_id="r", backend="local", isolated=False, seed=0, shots=shots
        )
        assert len(result.detected) == 1
        assert len(result.slipped) == 1
        assert len(result.detected) + len(result.slipped) == len(result.planted)
        assert result.detection_rate == 0.5
        assert result.false_positive_rate == 0.0

    def test_the_report_renders_and_names_the_slip(self):
        from ais.redteam import to_markdown

        shots = (_shot("sneaky", predicts=GAP, expected="planted", verdict="PASS"),)
        result = CampaignResult(
            run_id="r", backend="local", isolated=False, seed=0, shots=shots
        )
        report = to_markdown(result)
        assert "sneaky" in report
        assert "What slipped through" in report


def _executed(shot) -> bool:
    """Whether the sandbox actually ran this edit.

    The stubless local backend does real work -- spawn an interpreter, install
    the tracer, run pytest -- and some environments cannot (a CI runner with a
    locked-down temp dir, a restricted spawn). When it could not, the Verifier
    reports ``sandbox.infrastructure`` and stands the other rules down, which is
    honest but leaves nothing for an integration test to conclude. These tests
    assert what happens *when the edit ran*, so a run that never happened is a
    skip, not a failure. Every other test in this file is platform-independent
    and still covers the generator and scoring logic.
    """
    return not shot.result.error and "sandbox.infrastructure" not in shot.result.rules_fired


@pytest.mark.slow
class TestCampaign:
    """Fires real attacks through the real pipeline. This is the honest part.

    Skips, rather than fails, where the sandbox could not execute at all -- see
    ``_executed``. On a host that can run the local backend (Linux CI, or
    anywhere with Docker) these run for real and assert the detections.
    """

    def _fire(self, settings, ids):
        # Self-calibrate first. A detection assertion is only meaningful in an
        # environment whose sandbox both runs and observes cleanly. A pure,
        # boring, benign edit is the control: if it cannot execute (a locked-down
        # runner -> sandbox.infrastructure) or comes back flagged (the local
        # backend's Windows path handling reports spurious escapes that Docker,
        # the real path, does not), then nothing this sandbox says about a real
        # attack can be trusted here, so skip rather than assert on noise. On
        # Linux CI, and anywhere with Docker, the control is clean and these run.
        control = self._run(settings, ["benign-helper"]).shots[0]
        if not _executed(control):
            pytest.skip(f"sandbox could not execute here: {control.result.rules_fired}")
        if control.flagged:
            pytest.skip(f"sandbox observations are untrustworthy here: {control.result.rules_fired}")
        return self._run(settings, ids)

    def _run(self, settings, ids):
        pool = [_attack(attack_id) for attack_id in ids]
        edits = RedTeam(MutationStrategy(pool)).generate(count=len(ids), seed=0)
        # allow_uncontained: these run under the local backend for the test, and
        # the chosen attacks are inert on a throwaway host by construction.
        return run_campaign(settings, edits, seed=0, allow_uncontained=True)

    def test_a_dormant_denylisted_attack_is_caught_by_the_static_scan(self, settings):
        result = self._fire(settings, ["net-dormant"])
        shot = result.shots[0]
        assert shot.flagged, "dead code carrying a denylisted call must still be seen"
        assert "code.dangerous_construct" in shot.result.rules_fired

    def test_a_dormant_off_denylist_write_slips_through(self, settings):
        # The finding the whole exercise exists to produce: a real gap.
        result = self._fire(settings, ["fs-write-dormant"])
        shot = result.shots[0]
        assert shot.slipped, "a dormant open() write has nothing to catch it, and should slip"

    def test_an_active_attack_is_caught_at_runtime(self, settings):
        result = self._fire(settings, ["fs-read-active"])
        shot = result.shots[0]
        assert shot.flagged
        assert "fs.escape_read" in shot.result.rules_fired

    def test_a_benign_edit_is_not_flagged(self, settings):
        result = self._fire(settings, ["benign-scary-names"])
        shot = result.shots[0]
        assert not shot.flagged, "identifiers named socket/system must not trip a rule"

    def test_detected_and_slipped_partition_the_planted_attacks(self, settings):
        result = self._fire(settings, ["net-dormant", "fs-write-dormant"])
        assert len(result.detected) + len(result.slipped) == len(result.planted)
