"""The pristine sample-project files, and the ways an attack edits them.

An attack has to propose the *whole* content of a file, because that is what an
:class:`~ais.models.EditRequest` carries. These helpers start from the exact
bytes the Mediator will diff against -- the same template it seeds the mediated
project from -- so a generated edit is a real diff against real baseline
content, not an approximation of one.

``replace_in`` fails loudly when its anchor is missing. A silent no-op edit that
still claimed to be an attack would poison the campaign's numbers, and an
earlier version of this project shipped exactly that bug in a scenario.
"""

from __future__ import annotations

from pathlib import Path

from ais.patchkit import read_text_exact

#: The sample project the Mediator seeds from. Kept in lockstep with
#: ``Settings.paths.template_project`` by construction: both point here.
TEMPLATE = Path(__file__).resolve().parent.parent.parent / "sample_project"

_FILES = ("pricing.py", "inventory.py", "textkit.py", "tests/test_pricing.py")

#: Byte-exact source of each file an attack is allowed to touch.
SAMPLES: dict[str, str] = {
    rel: read_text_exact(str(TEMPLATE / rel)) for rel in _FILES
}


class InjectionError(Exception):
    """Raised when an attack cannot be applied to the sample it targets."""


def _append(base: str, code: str) -> str:
    """Add ``code`` at module scope, after the existing file."""
    body = base if base.endswith("\n") else base + "\n"
    return f"{body}\n\n{code.rstrip()}\n"


def inject_module_scope(base: str, code: str) -> str:
    """Append code that *runs* when the module is imported."""
    return _append(base, code)


def inject_dead_code(base: str, code: str) -> str:
    """Append code that is present but never executed (a helper nobody calls)."""
    return _append(base, code)


def replace_in(base: str, old: str, new: str) -> str:
    """Swap ``old`` for ``new`` exactly once, or refuse."""
    count = base.count(old)
    if count != 1:
        raise InjectionError(
            f"anchor appears {count} times, expected exactly 1: {old[:60]!r}"
        )
    return base.replace(old, new)
