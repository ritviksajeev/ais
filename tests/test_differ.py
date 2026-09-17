"""The behavioural probe: the second oracle.

Until this existed, AiS judged correctness only through the project's own test
suite, which made it exactly as good as those tests -- a wrong value on an input
nothing exercises was invisible, and that was written up as a structural limit.
It was not structural. The previous version of the code is an oracle too, and
these tests are about whether it is a trustworthy one: it has to notice a real
change, stay silent on a rewrite that preserves behaviour, and survive code that
misbehaves while being probed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

DIFFER = Path(__file__).resolve().parent.parent / "ais" / "sandbox" / "differ.py"


def probe(tmp_path, name: str, source: str) -> dict:
    """Run the probe over one module and return its record."""
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "mod.py").write_text(source, encoding="utf-8")
    out = tmp_path / f"{name}.json"
    subprocess.run(
        [sys.executable, str(DIFFER), "--root", str(root), "--targets", "mod.py",
         "--out", str(out), "--seed", "0"],
        check=True, capture_output=True, timeout=120,
    )
    return json.loads(out.read_text(encoding="utf-8"))


def divergences(before: dict, after: dict) -> list[str]:
    b, a = before["functions"], after["functions"]
    return [k for k in b if k in a and b[k] != a[k]]


BASE = '''
from __future__ import annotations


def scale(value: int) -> int:
    """Double, except large values are halved."""
    return value * 2


def label(text: str) -> str:
    return text.strip().lower()
'''


class TestDetection:
    def test_a_changed_return_value_is_seen(self, tmp_path):
        changed = BASE.replace("return value * 2", "return value * 3")
        assert divergences(probe(tmp_path, "b", BASE), probe(tmp_path, "a", changed))

    def test_a_change_on_a_narrow_input_range_is_seen(self, tmp_path):
        # The whole point: wrong only for big values, which no hand-written test
        # in the sample project happens to try.
        changed = BASE.replace(
            "    return value * 2",
            "    if value > 100_000:\n        return 0\n    return value * 2",
        )
        assert divergences(probe(tmp_path, "b2", BASE), probe(tmp_path, "a2", changed))

    def test_a_behaviour_preserving_rewrite_is_silent(self, tmp_path):
        # A verbose but equivalent body must not read as a change, or every
        # refactor becomes a finding and the signal is worthless.
        rewritten = BASE.replace(
            "    return value * 2",
            "    doubled = value + value\n    return doubled",
        )
        assert divergences(probe(tmp_path, "b3", BASE), probe(tmp_path, "a3", rewritten)) == []

    def test_a_newly_raised_exception_is_a_divergence(self, tmp_path):
        changed = BASE.replace(
            "    return value * 2",
            "    if value < 0:\n        raise ValueError(value)\n    return value * 2",
        )
        assert divergences(probe(tmp_path, "b4", BASE), probe(tmp_path, "a4", changed))

    def test_a_different_exception_type_is_a_divergence(self, tmp_path):
        raiser = "def f(value: int) -> int:\n    raise ValueError(value)\n"
        other = "def f(value: int) -> int:\n    raise TypeError(value)\n"
        assert divergences(probe(tmp_path, "b5", raiser), probe(tmp_path, "a5", other))

    def test_the_same_exception_type_is_not_a_divergence(self, tmp_path):
        # Messages routinely embed the argument being varied, so comparing them
        # would report a difference for every input rather than every change.
        one = 'def f(value: int) -> int:\n    raise ValueError(f"bad {value}")\n'
        two = 'def f(value: int) -> int:\n    raise ValueError(f"wrong {value}")\n'
        assert divergences(probe(tmp_path, "b6", one), probe(tmp_path, "a6", two)) == []


class TestScope:
    def test_private_functions_are_not_called(self, tmp_path):
        # Calling a project's private helpers means calling things the tests
        # never do, on arguments invented here. Public API only.
        source = "def _secret(value: int) -> int:\n    return value\n"
        assert probe(tmp_path, "p1", source)["functions"] == {}

    def test_functions_without_usable_annotations_are_skipped(self, tmp_path):
        source = "def mystery(thing):\n    return thing\n"
        assert probe(tmp_path, "p2", source)["functions"] == {}

    def test_a_default_stands_in_for_an_unannotated_parameter(self, tmp_path):
        source = "def greet(name: str, punct='!') -> str:\n    return name + punct\n"
        assert probe(tmp_path, "p3", source)["functions"]

    def test_imported_helpers_are_not_reprobed(self, tmp_path):
        # Otherwise a helper is probed once per module that imports it, and a
        # change to it is reported several times over.
        source = "from math import floor\n\n\ndef f(value: int) -> int:\n    return value\n"
        record = probe(tmp_path, "p4", source)["functions"]
        assert record and not any("floor" in key for key in record)


class TestRobustness:
    def test_an_unimportable_module_is_reported_not_fatal(self, tmp_path):
        record = probe(tmp_path, "r1", "import nonexistent_module_xyz\n")
        assert record["errors"]
        assert record["functions"] == {}

    def test_a_function_that_raises_on_every_input_still_records(self, tmp_path):
        record = probe(tmp_path, "r2", "def f(x: int) -> int:\n    raise RuntimeError\n")
        assert all(v.startswith("!RuntimeError") for v in record["functions"].values())

    def test_the_probe_is_deterministic(self, tmp_path):
        assert probe(tmp_path, "d1", BASE)["functions"] == probe(tmp_path, "d2", BASE)["functions"]


class TestInputs:
    def test_large_integers_are_not_all_round_numbers(self):
        # A planted bug that dropped the fractional part above $1000 went
        # undetected because every large candidate was a multiple of 100 and
        # both sides rendered "1000.00". Magnitude alone is not enough.
        from ais.sandbox.differ import CANDIDATES

        large = [v for v in CANDIDATES["int"] if v >= 100_000]
        assert large, "there must be values large enough to cross magnitude branches"
        assert any(v % 100 for v in large), "at least one large value must be ragged"


STATEFUL = """
from __future__ import annotations


class Ledger:
    \"\"\"A deliberately stateful object: later calls depend on earlier ones.\"\"\"

    def __init__(self) -> None:
        self._held: dict = {}

    def add(self, key: str, amount: int) -> None:
        self._held[key] = self._held.get(key, 0) + amount

    def total(self, key: str) -> int:
        return self._held.get(key, 0)

    def take(self, key: str, amount: int) -> int:
        held = self._held.get(key, 0)
        if amount > held:
            raise ValueError("not enough")
        self._held[key] = held - amount
        return self._held[key]
"""


class TestClassMethods:
    def test_a_stateful_sequence_is_probed(self, tmp_path):
        record = probe(tmp_path, "c1", STATEFUL)["functions"]
        assert record, "a class with public methods must be probed"
        assert any("Ledger" in key for key in record)

    def test_later_calls_see_what_earlier_ones_did(self, tmp_path):
        # The whole reason a sequence is used rather than one call at a time.
        # If arguments never collided, every `total` would answer 0 and the
        # probe would look busy while proving nothing.
        record = probe(tmp_path, "c2", STATEFUL)["functions"]
        totals = [v for k, v in record.items() if ".total(" in k]
        assert any(v not in ("0", "!KeyError") for v in totals), (
            "no call observed state left by an earlier one"
        )

    def test_a_bug_that_only_shows_after_state_accumulates_is_caught(self, tmp_path):
        broken = STATEFUL.replace(
            "        return self._held[key]\n", "        return self._held[key] - 1\n"
        )
        assert divergences(probe(tmp_path, "c3", STATEFUL), probe(tmp_path, "c4", broken))

    def test_rewriting_internal_state_is_not_a_behaviour_change(self, tmp_path):
        # The false-positive guard, and the reason internal state is never
        # fingerprinted. This stores a list of pairs instead of a dict and
        # behaves identically; a refactor like this must stay silent, or the
        # tool punishes exactly the clean-ups it should ignore.
        refactored = """
from __future__ import annotations


class Ledger:
    def __init__(self) -> None:
        self._pairs: list = []

    def _find(self, key: str) -> int:
        for index, (name, _amount) in enumerate(self._pairs):
            if name == key:
                return index
        return -1

    def add(self, key: str, amount: int) -> None:
        index = self._find(key)
        if index < 0:
            self._pairs.append((key, amount))
        else:
            self._pairs[index] = (key, self._pairs[index][1] + amount)

    def total(self, key: str) -> int:
        index = self._find(key)
        return 0 if index < 0 else self._pairs[index][1]

    def take(self, key: str, amount: int) -> int:
        held = self.total(key)
        if amount > held:
            raise ValueError("not enough")
        index = self._find(key)
        if index < 0:
            self._pairs.append((key, -amount))
        else:
            self._pairs[index] = (key, held - amount)
        return self.total(key)
"""
        assert divergences(probe(tmp_path, "c5", STATEFUL), probe(tmp_path, "c6", refactored)) == []

    def test_a_class_whose_method_set_changed_drops_out_of_the_comparison(self, tmp_path):
        # Adding a method shifts the round-robin, so step N on one side would be
        # compared against a differently-reached step N on the other. The keys
        # name the method set precisely so that stops matching rather than
        # inventing a divergence out of a reordering.
        widened = STATEFUL + "\n    def audit(self) -> int:\n        return len(self._held)\n"
        before = probe(tmp_path, "c7", STATEFUL)["functions"]
        after = probe(tmp_path, "c8", widened)["functions"]
        shared = [k for k in before if k in after and "Ledger" in k]
        assert shared == [], "a reshaped class must not be compared step by step"

    def test_private_methods_are_not_driven(self, tmp_path):
        record = probe(tmp_path, "c9", STATEFUL)["functions"]
        assert not any("._find(" in key or "._held" in key for key in record)

    def test_a_class_that_cannot_be_constructed_is_skipped(self, tmp_path):
        source = "class Needy:\n    def __init__(self, thing):\n        self.thing = thing\n\n    def go(self) -> int:\n        return 1\n"
        assert probe(tmp_path, "c10", source)["functions"] == {}

    def test_probing_a_class_is_deterministic(self, tmp_path):
        assert probe(tmp_path, "c11", STATEFUL)["functions"] == probe(tmp_path, "c12", STATEFUL)["functions"]


class TestSequenceCoverage:
    """Two lessons the red team taught this probe, kept as tests.

    Both were found the same way: a planted bug survived a campaign, and the
    reason was the probe rather than the rules.
    """

    def test_identifiers_are_drawn_from_a_tiny_pool(self):
        # Arguments used as keys must collide or state is never exercised:
        # add("x") is only interesting if something later touches "x".
        from ais.sandbox.differ import METHOD_CANDIDATES

        assert len(METHOD_CANDIDATES["str"]) <= 4

    def test_quantities_are_not_narrowed_to_match(self):
        # The opposite requirement, which an earlier version conflated with the
        # one above. Narrowing numbers too caps how much state can accumulate,
        # and a planted off-by-one above 100 units was unreachable however long
        # the sequence ran.
        from ais.sandbox.differ import METHOD_CANDIDATES

        assert max(METHOD_CANDIDATES["int"]) >= 250

    def test_several_trajectories_are_run_per_class(self):
        # One sequence is a single path through a state machine and which states
        # it reaches is luck. More trajectories cover more of the machine.
        from ais.sandbox.differ import SEQUENCE_RUNS

        assert SEQUENCE_RUNS > 1

    def test_each_trajectory_starts_from_a_fresh_instance(self, tmp_path):
        # Otherwise later runs inherit the wreckage of earlier ones and the
        # first call of run 2 is not comparable to the first call of run 1.
        record = probe(tmp_path, "s1", STATEFUL)["functions"]
        firsts = {k.split("#")[1] for k in record if "@0#00" in k or "@1#00" in k}
        assert firsts, "each run must record a step 00"

    def test_a_bug_beyond_a_magnitude_threshold_is_reached(self, tmp_path):
        # The concrete case: wrong only once an accumulated total passes 100.
        # A single short sequence with small values never gets there.
        source = STATEFUL.replace(
            "        self._held[key] = self._held.get(key, 0) + amount\n",
            "        self._held[key] = self._held.get(key, 0) + amount\n",
        )
        broken = STATEFUL.replace(
            "    def total(self, key: str) -> int:\n        return self._held.get(key, 0)\n",
            "    def total(self, key: str) -> int:\n"
            "        held = self._held.get(key, 0)\n"
            "        return held - 1 if held > 100 else held\n",
        )
        assert divergences(probe(tmp_path, "s2", source), probe(tmp_path, "s3", broken))
