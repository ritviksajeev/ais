"""Work out the smallest set of files a sandbox needs to exercise an edit.

Cloning the whole project into every sandbox would be simpler, and it would
also be a lie: the point of the Mediator is that an edit is evaluated against
the least context that can still meaningfully run it. This module computes that
context as a graph closure rather than a hand-maintained list, so it keeps
working as the project under edit grows.

The closure of a set of target files is:

* the targets themselves;
* every in-project module they import, transitively;
* every test module that transitively imports a target;
* the in-project modules those tests import, transitively;
* the harness files a test run needs regardless (``conftest.py``, config).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ais.patchkit import read_text_exact

#: Files always cloned, because without them the test command cannot run at all.
HARNESS_FILES = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")

#: Directory names never walked when building the module map. Matched against
#: the path *relative to the project root*: the root itself may well sit inside
#: a directory with one of these names (the live project lives under
#: ``.ais_run/``), and filtering on ancestors above the root would silently
#: empty the closure.
IGNORED_DIRS = frozenset({"__pycache__", ".git", ".pytest_cache", ".ais_run", ".venv"})


def _ignored(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(part in IGNORED_DIRS for part in relative.parts)


@dataclass(frozen=True)
class Closure:
    """The result of a closure computation: which files, and why each one."""

    #: Ordered path -> reason. Order is deterministic for reproducible sandboxes.
    reasons: dict[str, str]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(self.reasons)


def project_modules(root: Path) -> dict[str, str]:
    """Map importable module name -> project-relative path, for this project only.

    Both ``pkg/mod.py`` (as ``pkg.mod``) and flat ``mod.py`` (as ``mod``) are
    registered, because the sample project imports flatly via ``conftest.py``
    while a larger project would import by package path.
    """
    modules: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        if _ignored(path, root):
            continue
        relative = path.relative_to(root)
        dotted = ".".join(relative.with_suffix("").parts)
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        modules.setdefault(dotted, relative.as_posix())
        # A flat import of the basename, which is how a rootdir-on-sys.path
        # project (the common pytest layout) actually refers to its modules.
        modules.setdefault(relative.stem, relative.as_posix())
    return modules


def imported_names(source: str) -> set[str]:
    """Top-level module names a source file imports. Unparseable source yields none.

    A file that does not parse is not a reason to fail closure computation --
    the sandbox is exactly where a syntax error should surface, as a test-run
    failure the human can see, not as a Mediator crash.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: not resolvable by name alone
                continue
            if node.module:
                names.add(node.module)
                names.add(node.module.split(".")[0])
    return names


def _read(root: Path, relative: str, overrides: dict[str, str]) -> str | None:
    if relative in overrides:
        return overrides[relative]
    path = root / relative
    if not path.is_file():
        return None
    try:
        return read_text_exact(str(path))
    except (UnicodeDecodeError, OSError):
        return None


def _dependencies(
    root: Path, relative: str, modules: dict[str, str], overrides: dict[str, str]
) -> set[str]:
    source = _read(root, relative, overrides)
    if source is None:
        return set()
    return {
        modules[name]
        for name in imported_names(source)
        if name in modules and modules[name] != relative
    }


def _transitive(
    root: Path, seeds: Iterable[str], modules: dict[str, str], overrides: dict[str, str]
) -> set[str]:
    """Every in-project file reachable from ``seeds`` by imports, excluding seeds."""
    found: set[str] = set()
    queue = list(seeds)
    seen = set(queue)
    while queue:
        for dependency in sorted(_dependencies(root, queue.pop(), modules, overrides)):
            if dependency not in seen:
                seen.add(dependency)
                found.add(dependency)
                queue.append(dependency)
    return found


def test_files(root: Path) -> list[str]:
    """Project-relative paths of every test module, in sorted order."""
    found = []
    for path in sorted(root.rglob("test_*.py")):
        if _ignored(path, root):
            continue
        found.append(path.relative_to(root).as_posix())
    return found


def compute(root: Path, targets: Iterable[str], overrides: dict[str, str] | None = None) -> Closure:
    """Build the sandbox closure for ``targets``.

    ``overrides`` supplies proposed content for files the request creates or
    rewrites, so a brand-new file's imports are followed too -- an edit that
    adds ``import requests`` should pull in what it needs (or visibly fail to).
    """
    overrides = overrides or {}
    modules = project_modules(root)
    targets = list(dict.fromkeys(targets))

    reasons: dict[str, str] = {name: "edit target" for name in targets}

    for dependency in sorted(_transitive(root, targets, modules, overrides)):
        reasons.setdefault(dependency, "imported by an edit target")

    covering = _covering_tests(root, set(reasons), modules, overrides)
    for test in covering:
        reasons.setdefault(test, "test that exercises an edit target")

    for dependency in sorted(_transitive(root, covering, modules, overrides)):
        reasons.setdefault(dependency, "imported by a covering test")

    for harness in HARNESS_FILES:
        if (root / harness).is_file():
            reasons.setdefault(harness, "test harness file")

    # Sort so two runs of the same request build byte-identical sandboxes.
    return Closure(reasons=dict(sorted(reasons.items())))


def _covering_tests(
    root: Path, in_scope: set[str], modules: dict[str, str], overrides: dict[str, str]
) -> list[str]:
    """Tests that transitively import anything already in scope."""
    covering = []
    for test in test_files(root):
        if test in in_scope:
            covering.append(test)
            continue
        reachable = _transitive(root, [test], modules, overrides) | {test}
        if reachable & in_scope:
            covering.append(test)
    return covering
