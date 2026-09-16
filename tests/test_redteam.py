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

from ais.redteam import RedTeam, ATTACKS, families, run_campaign
from ais.redteam.generator import MutationStrategy
from ais.redteam.library import Attack, CAUGHT, GAP
from ais.redteam.samples import InjectionError, SAMPLES, replace_in


def _attack(attack_id: str) -> Attack:
    return next(a for a in ATTACKS if a.id == attack_id)


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


@pytest.mark.slow
class TestCampaign:
    """Fires real attacks through the real pipeline. This is the honest part."""

    def _campaign(self, settings, ids):
        pool = [_attack(attack_id) for attack_id in ids]
        edits = RedTeam(MutationStrategy(pool)).generate(count=len(ids), seed=0)
        # allow_uncontained: these run under the local backend for the test, and
        # the chosen attacks are inert on a throwaway host by construction.
        return run_campaign(settings, edits, seed=0, allow_uncontained=True)

    def test_a_dormant_denylisted_attack_is_caught_by_the_static_scan(self, settings):
        result = self._campaign(settings, ["net-dormant"])
        shot = result.shots[0]
        assert shot.flagged, "dead code carrying a denylisted call must still be seen"
        assert "code.dangerous_construct" in shot.result.rules_fired

    def test_a_dormant_off_denylist_write_slips_through(self, settings):
        # The finding the whole exercise exists to produce: a real gap.
        result = self._campaign(settings, ["fs-write-dormant"])
        shot = result.shots[0]
        assert shot.slipped, "a dormant open() write has nothing to catch it, and should slip"

    def test_an_active_attack_is_caught_at_runtime(self, settings):
        result = self._campaign(settings, ["fs-read-active"])
        shot = result.shots[0]
        assert shot.flagged
        assert "fs.escape_read" in shot.result.rules_fired

    def test_a_benign_edit_is_not_flagged(self, settings):
        result = self._campaign(settings, ["benign-scary-names"])
        shot = result.shots[0]
        assert not shot.flagged, "identifiers named socket/system must not trip a rule"

    def test_detected_and_slipped_partition_the_planted_attacks(self, settings):
        result = self._campaign(settings, ["net-dormant", "fs-write-dormant"])
        assert len(result.detected) + len(result.slipped) == len(result.planted)
