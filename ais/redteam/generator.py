"""Turn the attack catalogue into a blind campaign of edit requests.

A campaign is deterministic in its seed: the same seed produces the same edits,
so a run is reproducible and a regression is bisectable, while a different seed
explores different concrete details (which host, which temp path, where the
module name is cut). That mirrors how the scenarios stay fixed for the headline
demo while this explores around them.

The ground-truth fields (``expected``, ``expect_rules``) ride along on each
:class:`~ais.models.EditRequest` exactly as the scenarios' do, and are dropped
before the Verifier sees them by the same code path -- see
``ais/verifier/verifier.py``. The red team knows the answers; the Verifier does
not.

``Strategy`` is the seam a live model would occupy. The only strategy today is
:class:`MutationStrategy`, which samples the hand-written catalogue; a
``ModelStrategy`` that asked an LLM for novel edits would implement the same one
method and nothing downstream would change.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Protocol

from ais.models import EditRequest
from ais.redteam.library import ATTACKS, Attack


@dataclass(frozen=True)
class GeneratedEdit:
    """One generated edit, plus the red team's private notes about it."""

    request: EditRequest
    attack_id: str
    family: str
    technique: str
    predicts: str
    intent: str
    detail: str


class Strategy(Protocol):
    """Produces attacks to instantiate. The seam a model would plug into."""

    def choose(self, rng: random.Random, count: int) -> list[Attack]:
        ...


class MutationStrategy:
    """Sample the hand-written catalogue, covering every attack before repeating.

    Coverage first: with fewer requests than attacks a caller still wants a
    spread across families rather than five variants of one socket, and with
    more, the extra draws are random so concrete details vary.
    """

    def __init__(self, pool: Iterable[Attack] = ATTACKS) -> None:
        self.pool = list(pool)

    def choose(self, rng: random.Random, count: int) -> list[Attack]:
        chosen: list[Attack] = []
        shuffled = self.pool[:]
        rng.shuffle(shuffled)
        while len(chosen) < count:
            if not shuffled:
                shuffled = self.pool[:]
                rng.shuffle(shuffled)
            chosen.append(shuffled.pop())
        return chosen


class RedTeam:
    """Generates a campaign of edit requests from a strategy and a seed."""

    def __init__(self, strategy: Strategy | None = None) -> None:
        self.strategy = strategy or MutationStrategy()

    def generate(self, count: int = 12, seed: int = 0) -> list[GeneratedEdit]:
        rng = random.Random(seed)
        edits: list[GeneratedEdit] = []
        for index, attack in enumerate(self.strategy.choose(rng, count)):
            payload = attack.build(rng)
            request_id = f"rt-{seed}-{index:02d}-{attack.id}"
            request = EditRequest(
                request_id=request_id,
                title=_title(attack, payload.detail),
                rationale=payload.detail,
                targets=payload.targets,
                proposed=payload.proposed,
                expected=attack.expected,
                expect_rules=attack.expect_rules,
                note=attack.intent,
            )
            edits.append(
                GeneratedEdit(
                    request=request,
                    attack_id=attack.id,
                    family=attack.family,
                    technique=attack.technique,
                    predicts=attack.predicts,
                    intent=attack.intent,
                    detail=payload.detail,
                )
            )
        return edits


def _title(attack: Attack, detail: str) -> str:
    """A plausible-looking edit title -- the kind a real agent would write.

    Deliberately does not announce the attack. The reviewer (and the Verifier)
    should have to work it out from behaviour, not read it off the label.
    """
    return detail[0].upper() + detail[1:] if detail else attack.id
