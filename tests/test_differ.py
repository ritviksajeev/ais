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
