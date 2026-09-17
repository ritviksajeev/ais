"""Probe a module's observable behaviour, so two versions can be compared.

AiS judges an edit by running it, and until now the only oracle was the project's
own test suite. That makes the verifier exactly as good as the tests: a wrong
value on an input no test exercises is invisible, which the red team demonstrated
and which was written up as a structural limit.

It is not structural. There is a second oracle sitting in the sandbox already --
*the previous version of the code*. This script makes it usable: it imports the
module, calls every public function it can build arguments for, and writes down
what came back. Run it once against the baseline and once against the patched
workspace, compare the two records, and any difference is a behaviour change on
an input nothing had to think to write down.

Run as a subprocess, once per version, each with its own ``sys.path``. Two
versions of ``pricing`` cannot both be ``pricing`` in one interpreter, and the
modules in a project import each other by name -- loading both in-process would
silently mix a baseline module with a patched one. Separate processes make that
impossible rather than merely unlikely.

A divergence is evidence, not a verdict. Plenty of legitimate edits change
behaviour on purpose; that is what the reviewer is for.

Stdlib only, and copied into the sandbox beside the runner.
"""

from __future__ import annotations

import argparse
import inspect
import json
import random
import sys
from pathlib import Path

#: Candidate arguments per annotated type. Small, fixed and deliberately
#: boring -- boundaries, signs, zero, and a couple of values large enough to
#: cross the magnitude thresholds real code tends to branch on.
#:
#: The large integers are deliberately *untidy*. The first version of this list
#: used 100_000 and 250_000, and a planted bug that dropped the fractional part
#: above $1000 went undetected: with no cents to drop, both sides render
#: "1000.00". A probe only finds a difference on an input where the difference
#: is visible, so magnitude alone is not enough -- the value also has to be
#: ragged in whatever way the code under test divides on.
CANDIDATES: dict[str, list] = {
    "int": [0, 1, 2, 7, 99, 100, 101, 999, 1000, 1234, 1999,
            100_050, 123_456, 999_999, -1, -100],
    "float": [0.0, 0.5, 1.5, 2.5, 10.0, 25.0, 33.5, 50.0, 99.9, 100.0, -0.5],
    "bool": [True, False],
    "str": ["", "a", "hello world", "  padded  ", "Ünïcode", "a-b-c", "The Quick Brown Fox"],
}

#: Longest repr recorded for a single value. A divergence is about *whether*
#: two runs differ, so an enormous return value only needs to differ detectably.
MAX_REPR = 300


def _type_name(annotation: object) -> str | None:
    """The simple type name of an annotation, or None if we cannot use it.

    Annotations arrive as strings under ``from __future__ import annotations``,
    which is exactly how the sample project is written, so the string form is
    the normal case rather than the exotic one.
    """
    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, type):
        return annotation.__name__
    text = str(annotation).strip()
    return text if text in CANDIDATES else None


def _argument_space(function) -> list[list] | None:
    """Candidate values for each parameter, or None if the signature is not probeable."""
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return None

    space: list[list] = []
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            return None  # *args/**kwargs: no honest way to enumerate
        name = _type_name(parameter.annotation)
        if name is not None:
            space.append(list(CANDIDATES[name]))
        elif parameter.default is not inspect.Parameter.empty:
            space.append([parameter.default])  # unannotated but defaulted: use it
        else:
            return None
    return space


def _combinations(space: list[list], limit: int, rng: random.Random) -> list[tuple]:
    """Up to ``limit`` argument tuples, deterministically chosen."""
    if not space:
        return [()]
    total = 1
    for values in space:
        total *= len(values)

    def at(index: int) -> tuple:
        args = []
        for values in space:
            index, position = divmod(index, len(values))
            args.append(values[position])
        return tuple(args)

    if total <= limit:
        return [at(i) for i in range(total)]
    # Sorted so the selection is stable regardless of set iteration order.
    return [at(i) for i in sorted(rng.sample(range(total), limit))]


def _outcome(function, args: tuple) -> str:
    """What calling ``function(*args)`` produced, as a comparable string.

    An exception is recorded by type alone. Messages routinely embed the very
    values being varied, so comparing them would report a difference for every
    input rather than for every behaviour change.
    """
    try:
        value = function(*args)
    except Exception as exc:  # noqa: BLE001 -- raising *is* an observable outcome
        return f"!{type(exc).__name__}"
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001
        return "<unrepresentable>"
    return text if len(text) <= MAX_REPR else text[: MAX_REPR - 3] + "..."


def probe_module(module, limit: int, rng: random.Random) -> dict[str, str]:
    """Outcome per ``function(args)`` for every public function we can call."""
    records: dict[str, str] = {}
    for name, function in sorted(vars(module).items()):
        if name.startswith("_") or not inspect.isfunction(function):
            continue
        # Only functions this module defines. Imported helpers belong to their
        # own module and would otherwise be probed once per importer.
        if getattr(function, "__module__", None) != module.__name__:
            continue
        space = _argument_space(function)
        if space is None:
            continue
        for args in _combinations(space, limit, rng):
            records[f"{name}{args!r}"] = _outcome(function, args)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="AiS behavioural probe")
    parser.add_argument("--root", required=True, help="directory to import the modules from")
    parser.add_argument("--targets", default="", help="comma-separated module files")
    parser.add_argument("--out", required=True, help="where to write the probe record")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--limit", type=int, default=24, help="max argument tuples per function")
    arguments = parser.parse_args()

    root = Path(arguments.root).resolve()
    sys.path.insert(0, str(root))

    record: dict = {"functions": {}, "errors": {}}
    for target in [t.strip() for t in arguments.targets.split(",") if t.strip()]:
        if not target.endswith(".py"):
            continue
        name = Path(target).stem
        try:
            module = __import__(name)
        except Exception as exc:  # noqa: BLE001 -- an unimportable side is reported, not fatal
            record["errors"][name] = f"{type(exc).__name__}: {exc}"
            continue
        try:
            record["functions"].update(
                {f"{name}.{k}": v for k, v in probe_module(module, arguments.limit, random.Random(arguments.seed)).items()}
            )
        except Exception as exc:  # noqa: BLE001
            record["errors"][name] = f"{type(exc).__name__}: {exc}"

    Path(arguments.out).write_text(json.dumps(record, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
