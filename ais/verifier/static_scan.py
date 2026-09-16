"""A narrow static screen over the lines an edit *adds*.

The runtime trace is the primary evidence in AiS, and it is strictly better:
it records what the code did rather than what it looks like. But a trace only
covers paths the test suite actually executed. A ``socket.connect`` sitting in
a branch no test reaches leaves no trace at all, and a reviewer should still
see it.

So this is a complement, not a substitute, and it is kept deliberately small:
a fixed denylist of calls and imports, matched on the AST of the proposed file,
reported only when the construct sits on a line this edit introduced.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

#: Dotted call names worth surfacing next to a diff, and why.
DANGEROUS_CALLS: dict[str, str] = {
    "os.system": "runs a shell command",
    "os.popen": "runs a shell command",
    "os.execv": "replaces the process image",
    "os.execve": "replaces the process image",
    "os.remove": "deletes a file",
    "os.unlink": "deletes a file",
    "os.rmdir": "removes a directory",
    "os.chmod": "changes file permissions",
    "os.setuid": "changes process identity",
    "shutil.rmtree": "deletes a directory tree",
    "subprocess.run": "spawns a process",
    "subprocess.call": "spawns a process",
    "subprocess.Popen": "spawns a process",
    "subprocess.check_output": "spawns a process",
    "socket.socket": "opens a network socket",
    "socket.create_connection": "opens a network connection",
    "urllib.request.urlopen": "makes an HTTP request",
    "requests.get": "makes an HTTP request",
    "requests.post": "makes an HTTP request",
    "ctypes.CDLL": "loads a native library",
    "ctypes.cdll.LoadLibrary": "loads a native library",
    "pickle.loads": "deserialises untrusted data",
    "marshal.loads": "deserialises untrusted data",
    "eval": "evaluates a dynamically built expression",
    "exec": "executes dynamically built code",
    "__import__": "imports a module chosen at runtime",
    "importlib.import_module": "imports a module chosen at runtime",
    "compile": "compiles code at runtime",
    # Filesystem mutation that leaves no runtime trace when it sits in a branch
    # no test runs. The runtime tracer catches these when they execute; the red
    # team showed a dormant one had nothing to catch it, so the denylist names
    # them too.
    "shutil.copy": "copies a file",
    "shutil.copy2": "copies a file",
    "shutil.copyfile": "copies a file",
    "shutil.copytree": "copies a directory tree",
    "shutil.move": "moves a file or tree",
    "os.rename": "renames or moves a path",
    "os.replace": "replaces a path",
    "os.link": "creates a hard link",
    "os.symlink": "creates a symbolic link",
    "os.truncate": "truncates a file",
}

#: Method names distinctive to :class:`pathlib.Path`'s mutating API. Matched on
#: the attribute alone, because ``Path(x).unlink()`` reaches the call through a
#: receiver the dotted-name walk cannot name. Chosen to not collide with common
#: builtins -- ``str``/``dict``/``list`` have no ``unlink`` or ``write_text`` --
#: so matching by bare name does not misfire on ordinary code.
DANGEROUS_METHODS: dict[str, str] = {
    "unlink": "deletes a file (pathlib)",
    "rmdir": "removes a directory (pathlib)",
    "write_text": "writes a file (pathlib)",
    "write_bytes": "writes a file (pathlib)",
}

#: Mode characters that mean an ``open`` call can write, not merely read.
_WRITE_MODES = frozenset("wax+")

#: Imports whose mere presence in an added line is worth a reviewer's glance.
DANGEROUS_IMPORTS: dict[str, str] = {
    "socket": "network access",
    "ssl": "network access",
    "subprocess": "process execution",
    "ctypes": "native code loading",
    "requests": "network access",
    "urllib.request": "network access",
    "http.client": "network access",
    "ftplib": "network access",
    "smtplib": "network access",
    "telnetlib": "network access",
    "pickle": "untrusted deserialisation",
    "marshal": "untrusted deserialisation",
    "pty": "terminal control",
    "mmap": "raw memory mapping",
}


@dataclass(frozen=True)
class StaticFinding:
    path: str
    line: int
    construct: str
    why: str
    source: str

    def describe(self) -> str:
        return f"{self.path}:{self.line}  {self.construct} -- {self.why}\n    {self.source.strip()}"


def dotted_name(node: ast.AST) -> str | None:
    """Reconstruct a dotted call target, e.g. ``os.path.join``, or ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def scan(path: str, source: str, added_lines: set[int]) -> list[StaticFinding]:
    """Denylisted constructs introduced by this edit, in source order."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        line = exc.lineno or 0
        return [
            StaticFinding(
                path=path,
                line=line,
                construct="SyntaxError",
                why=f"the proposed file does not parse: {exc.msg}",
                source=_line(source, line),
            )
        ]

    findings: list[StaticFinding] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line is None or line not in added_lines:
            continue

        if isinstance(node, ast.Call):
            match = _dangerous_call(node)
            if match is not None:
                construct, why = match
                findings.append(StaticFinding(path, line, construct, why, _line(source, line)))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for module in _imported_modules(node):
                if module in DANGEROUS_IMPORTS:
                    findings.append(
                        StaticFinding(path, line, f"import {module}", DANGEROUS_IMPORTS[module], _line(source, line))
                    )
                    break

    return sorted(findings, key=lambda f: (f.line, f.construct))


def _dangerous_call(node: ast.Call) -> tuple[str, str] | None:
    """Classify one call, by dotted name, by method name, or as a writing ``open``.

    Three shapes, because an attacker gets to choose which one to use:
    ``shutil.copy(...)`` names its module, ``Path(x).unlink()`` hides behind a
    receiver the dotted-name walk cannot reconstruct, and ``open(x, "w")`` is an
    ordinary builtin that is only interesting depending on its arguments.
    """
    name = dotted_name(node.func)
    # Match the full dotted name and its trailing form, so both
    # ``import os; os.system(...)`` and ``from os import system`` land.
    for candidate in _candidates(name):
        if candidate in DANGEROUS_CALLS:
            return f"{candidate}()", DANGEROUS_CALLS[candidate]

    if isinstance(node.func, ast.Attribute) and node.func.attr in DANGEROUS_METHODS:
        return f".{node.func.attr}()", DANGEROUS_METHODS[node.func.attr]

    if isinstance(node.func, ast.Name) and node.func.id == "open":
        why = _write_mode(node)
        if why is not None:
            return "open()", why
    return None


def _write_mode(node: ast.Call) -> str | None:
    """Why this ``open`` call is worth flagging, or ``None`` if it only reads.

    Reading is ordinary and flagging it would bury the reviewer, so a call with
    no mode argument (or an explicit read mode) stays quiet. A mode the scanner
    cannot read statically is reported rather than assumed harmless: an edit
    that computes its own open mode is exactly the shape worth a second look.
    """
    mode: ast.expr | None = None
    if len(node.args) >= 2:
        mode = node.args[1]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value

    if mode is None:
        return None
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        if _WRITE_MODES & set(mode.value):
            return f"opens a file for writing (mode {mode.value!r})"
        return None
    return "opens a file with a mode computed at runtime"


def _candidates(name: str | None) -> list[str]:
    if not name:
        return []
    parts = name.split(".")
    return [name] + [".".join(parts[index:]) for index in range(1, len(parts))]


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level or not node.module:
        return []
    return [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]


def _line(source: str, number: int) -> str:
    lines = source.split("\n")
    return lines[number - 1] if 0 < number <= len(lines) else ""
