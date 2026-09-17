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


def newline_of(base: str) -> str:
    """The line ending this file already uses.

    Attacks are authored with ``\\n`` anchors, but the file on disk is whatever
    the checkout produced. A clone made before this repository pinned ``*.py``
    to LF still has CRLF, and ``git pull`` does not renormalise files a pull did
    not otherwise touch -- so a perfectly ordinary working copy can be CRLF
    while the anchors are LF. Matching then finds nothing and generation dies.

    Reading the file's own convention and translating the anchor to it keeps the
    red team working on any checkout, and keeps the proposed content in the same
    line endings as the baseline it will be diffed against -- which matters,
    because a mismatch would render every line of the file as changed.
    """
    return "\r\n" if "\r\n" in base else "\n"


def _to(newline: str, text: str) -> str:
    """Re-express LF-authored ``text`` in ``newline``."""
    return text if newline == "\n" else text.replace("\n", newline)


def _append(base: str, code: str) -> str:
    """Add ``code`` at module scope, after the existing file."""
    newline = newline_of(base)
    body = base if base.endswith(("\n", "\r")) else base + newline
    blank, block = newline, _to(newline, code.rstrip())
    return f"{body}{blank}{blank}{block}{newline}"


def inject_module_scope(base: str, code: str) -> str:
    """Append code that *runs* when the module is imported."""
    return _append(base, code)


def inject_dead_code(base: str, code: str) -> str:
    """Append code that is present but never executed (a helper nobody calls)."""
    return _append(base, code)


def replace_in(base: str, old: str, new: str) -> str:
    """Swap ``old`` for ``new`` exactly once, or refuse.

    The anchor is written with ``\\n`` and matched against the file's own line
    endings; see :func:`newline_of`.
    """
    newline = newline_of(base)
    anchor, replacement = _to(newline, old), _to(newline, new)
    count = base.count(anchor)
    if count != 1:
        raise InjectionError(
            f"anchor appears {count} times, expected exactly 1: {old[:60]!r}"
        )
    return base.replace(anchor, replacement)
