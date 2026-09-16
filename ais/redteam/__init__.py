"""Phase 0: an adversary that generates edits the rule set has never seen.

The ten scenarios in ``scenarios/`` were written by the same person who wrote
the rules, so 100% detection on them measures internal consistency, not
robustness -- every input was built to match a rule that already exists. This
package exists to produce the first number that could have come out differently:
edits generated against the *sample project* rather than against the rule list,
run blind through the real pipeline, and scored by the same ground-truth
machinery the scenarios use.

It is built to *fail honestly*. Some generated attacks are ones the runtime
tracer must catch no matter how their source is dressed up -- those validate the
core claim that observation beats inspection. Others are built specifically to
slip past the current rules: a dangerous capability that never executes during
the test run and is not on the static denylist has nothing to catch it, and the
campaign is supposed to surface exactly that as a miss, with a reproduction.

The generator here is a deterministic mutation strategy. The same seam would
take a live model as the adversary instead; that is deliberately left as the
next step, and :class:`~ais.redteam.generator.Strategy` is where it would plug
in.
"""

from ais.redteam.library import Attack, ATTACKS, families
from ais.redteam.generator import RedTeam, GeneratedEdit
from ais.redteam.campaign import Campaign, CampaignResult, run_campaign, to_markdown

__all__ = [
    "Attack",
    "ATTACKS",
    "families",
    "RedTeam",
    "GeneratedEdit",
    "Campaign",
    "CampaignResult",
    "run_campaign",
    "to_markdown",
]
