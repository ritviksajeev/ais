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
}

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
            name = dotted_name(node.func)
            # Match the full dotted name and its trailing form, so both
            # ``import os; os.system(...)`` and ``from os import system`` land.
            for candidate in _candidates(name):
                if candidate in DANGEROUS_CALLS:
                    findings.append(
                        StaticFinding(path, line, f"{candidate}()", DANGEROUS_CALLS[candidate], _line(source, line))
                    )
                    break
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for module in _imported_modules(node):
                if module in DANGEROUS_IMPORTS:
                    findings.append(
                        StaticFinding(path, line, f"import {module}", DANGEROUS_IMPORTS[module], _line(source, line))
                    )
                    break

    return sorted(findings, key=lambda f: (f.line, f.construct))


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
