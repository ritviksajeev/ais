"""Generate and apply unified diffs, with no third-party dependencies.

This module is copied verbatim into every sandbox. That is deliberate: the
patch the Verifier executes and the patch the Mediator later commits to the
real file are applied by *the same* code, so "what was tested" and "what was
applied" cannot drift apart because two patch implementations disagreed about
a fuzzy hunk.

The applier is intentionally strict -- exact context matching, no fuzz, no
offset search. A patch that does not apply cleanly is an error, never a guess.
"""

from __future__ import annotations

import difflib
import hashlib
import os
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

DEV_NULL = "/dev/null"
NO_NEWLINE_MARKER = "\\ No newline at end of file"


class PatchError(Exception):
    """Raised when a patch is malformed or does not apply cleanly."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text_exact(path: str) -> str:
    """Read a file without universal-newline translation.

    Python's default text mode rewrites ``\r\n`` to ``\n`` on the way in and
    back again on the way out. For a tool whose whole claim is "the bytes you
    approved are the bytes that were written", that silent rewrite is a defect:
    a CRLF file would come back from the pipeline with different content than
    it went in with. ``newline=""`` turns the translation off.
    """
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text_exact(path: str, text: str) -> None:
    """Write a file without universal-newline translation. See :func:`read_text_exact`."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------


def make_file_patch(path: str, old: str | None, new: str | None, context: int = 3) -> str:
    """Unified diff for one file. ``None`` means the file does not exist."""
    if old is None and new is None:
        raise PatchError(f"{path}: cannot diff a file that exists on neither side")
    old_lines = _split(old) if old is not None else []
    new_lines = _split(new) if new is not None else []
    if old_lines == new_lines:
        return ""

    from_label = DEV_NULL if old is None else f"a/{path}"
    to_label = DEV_NULL if new is None else f"b/{path}"
    diff = difflib.unified_diff(
        old_lines, new_lines, fromfile=from_label, tofile=to_label, n=context
    )
    body = "".join(_terminate(line) for line in diff)
    if not body:
        return ""
    return f"diff --git a/{path} b/{path}\n{body}"


def _terminate(line: str) -> str:
    """Newline-terminate a diff line, marking one that had no newline of its own.

    ``difflib.unified_diff`` emits a content line without a trailing newline
    when the source line had none, which glues it to whatever comes next. Git's
    own diffs solve this with an explicit marker; so do ours.
    """
    if line.endswith("\n"):
        return line
    return f"{line}\n{NO_NEWLINE_MARKER}\n"


def make_patch(changes: Mapping[str, tuple[str | None, str | None]], context: int = 3) -> str:
    """Unified diff across several files, in sorted path order for determinism."""
    parts = [
        make_file_patch(path, *changes[path], context=context) for path in sorted(changes)
    ]
    return "".join(p for p in parts if p)


def _split(text: str) -> list[str]:
    """Split on newlines only, keeping them, so content round-trips exactly.

    ``str.splitlines`` also breaks on ``\r``, ``\v``, ``\f`` and ``\u2028``.
    Using it here would mangle CRLF files and, worse, let a file smuggle extra
    "lines" past a parser that disagrees about where lines end. Splitting on
    ``\n`` alone keeps every other byte inside the line it belongs to.
    """
    if not text:
        return []
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)


@dataclass
class FilePatch:
    path: str
    creates: bool
    deletes: bool
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added_lines(self) -> list[str]:
        return [l[1:] for h in self.hunks for l in h.lines if l.startswith("+")]

    @property
    def removed_lines(self) -> list[str]:
        return [l[1:] for h in self.hunks for l in h.lines if l.startswith("-")]

    def added_line_numbers(self) -> set[int]:
        """Line numbers, in the *new* file, that this patch adds.

        The static scanner uses this to report only constructs the edit itself
        introduced. Flagging a dangerous call that was already in the file would
        turn every edit to that file into a finding about its neighbours.
        """
        numbers: set[int] = set()
        for hunk in self.hunks:
            line_number = hunk.new_start
            for raw in hunk.lines:
                if raw.startswith("\\"):
                    continue
                if raw.startswith("+"):
                    numbers.add(line_number)
                    line_number += 1
                elif raw.startswith(" "):
                    line_number += 1
        return numbers


def parse_patch(text: str) -> list[FilePatch]:
    """Parse a unified diff into per-file patches.

    Understands the subset this project generates: ``diff --git`` headers,
    ``---``/``+++`` file lines, and ``@@`` hunks. Anything else is rejected
    rather than silently skipped.
    """
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    lines = _diff_lines(text)
    index = 0

    while index < len(lines):
        line = lines[index]

        if line.startswith("diff --git "):
            current, hunk = None, None
            index += 1
            continue

        if line.startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise PatchError(f"'---' header at line {index + 1} has no '+++' partner")
            from_file = line[4:].strip()
            to_file = lines[index + 1][4:].strip()
            creates = from_file == DEV_NULL
            deletes = to_file == DEV_NULL
            path = _strip_prefix(to_file if not deletes else from_file)
            current = FilePatch(path=path, creates=creates, deletes=deletes)
            files.append(current)
            hunk = None
            index += 2
            continue

        if line.startswith("@@"):
            if current is None:
                raise PatchError(f"hunk at line {index + 1} precedes any file header")
            hunk = _parse_hunk_header(line, index + 1)
            current.hunks.append(hunk)
            index += 1
            continue

        if hunk is not None:
            if line.startswith(("+", "-", " ")):
                hunk.lines.append(line)
            elif line == "":
                # An empty line in a hunk body is a context line whose trailing
                # space was stripped in transit; treat it as blank context.
                hunk.lines.append(" ")
            elif line.startswith("\\"):
                hunk.lines.append(line)  # "\ No newline at end of file"
            else:
                hunk = None
                continue
        index += 1

    if not files:
        raise PatchError("patch contains no file headers")
    return files


def _diff_lines(text: str) -> list[str]:
    """Split a patch into lines on ``\n`` only, dropping the trailing empty cell.

    Same reasoning as :func:`_split`: the patch parser and the content splitter
    must agree byte-for-byte on what a line is.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _parse_hunk_header(line: str, lineno: int) -> Hunk:
    try:
        header = line.split("@@")[1].strip()
        old_part, new_part = header.split(" ")
        old_start, old_count = _parse_range(old_part.lstrip("-"))
        new_start, new_count = _parse_range(new_part.lstrip("+"))
    except (IndexError, ValueError) as exc:
        raise PatchError(f"malformed hunk header at line {lineno}: {line!r}") from exc
    return Hunk(old_start, old_count, new_start, new_count)


def _parse_range(part: str) -> tuple[int, int]:
    if "," in part:
        start, count = part.split(",", 1)
        return int(start), int(count)
    return int(part), 1


def _strip_prefix(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------


def apply_to_text(original: str | None, file_patch: FilePatch) -> str | None:
    """Apply one file's hunks to its content. Returns ``None`` for a deletion."""
    if file_patch.deletes:
        if original is None:
            raise PatchError(f"{file_patch.path}: cannot delete a file that is absent")
        return None
    if file_patch.creates and original not in (None, ""):
        raise PatchError(f"{file_patch.path}: patch creates a file that already exists")

    source = _split(original or "")
    result: list[str] = []
    cursor = 0  # 0-based index into ``source``

    for hunk in file_patch.hunks:
        start = max(hunk.old_start - 1, 0)
        if start < cursor:
            raise PatchError(f"{file_patch.path}: hunks are out of order at @@{hunk.old_start}")
        if start > len(source):
            raise PatchError(
                f"{file_patch.path}: hunk at line {hunk.old_start} is past end of file "
                f"({len(source)} lines)"
            )
        result.extend(source[cursor:start])
        cursor = start

        for position, raw in enumerate(hunk.lines):
            if raw.startswith("\\"):
                continue  # consumed via _lacks_newline by the line it follows
            tag, payload = raw[0], raw[1:]
            if tag in " -":
                actual = source[cursor] if cursor < len(source) else None
                _require_context(file_patch.path, hunk, cursor, payload, actual)
                if tag == " ":
                    result.append(source[cursor])
                cursor += 1
            elif tag == "+":
                if _lacks_newline(hunk.lines, position):
                    result.append(payload.rstrip("\n"))
                else:
                    result.append(payload if payload.endswith("\n") else payload + "\n")
            else:
                raise PatchError(f"{file_patch.path}: unexpected hunk line {raw!r}")

    result.extend(source[cursor:])
    return "".join(result)


def _lacks_newline(lines: Sequence[str], position: int) -> bool:
    """True when the hunk line at ``position`` is marked as unterminated."""
    following = position + 1
    return following < len(lines) and lines[following].startswith("\\")


def _require_context(path: str, hunk: Hunk, cursor: int, expected: str, actual: str | None) -> None:
    if actual is None:
        raise PatchError(
            f"{path}: hunk @@{hunk.old_start} ran past end of file at line {cursor + 1}"
        )
    if actual.rstrip("\n") != expected.rstrip("\n"):
        raise PatchError(
            f"{path}: context mismatch at line {cursor + 1}\n"
            f"  patch expects: {expected.rstrip(chr(10))!r}\n"
            f"  file contains: {actual.rstrip(chr(10))!r}"
        )


def apply_patch(root: str, patch_text: str) -> list[str]:
    """Apply a multi-file patch under ``root``. Returns the paths it changed.

    Every target path is confined to ``root``: a patch naming ``../secrets`` or
    an absolute path is rejected before a single byte is written. This is the
    last line of defence for the ``apply`` step -- the Mediator checks scope
    first, but the applier refuses to be the one that escapes.
    """
    root_real = os.path.realpath(root)
    file_patches = parse_patch(patch_text)

    staged: list[tuple[str, str, str | None]] = []
    for file_patch in file_patches:
        target = _resolve_within(root_real, file_patch.path)
        original = None
        if os.path.exists(target):
            original = read_text_exact(target)
        elif not file_patch.creates:
            raise PatchError(f"{file_patch.path}: patch modifies a file that is absent")
        staged.append((file_patch.path, target, apply_to_text(original, file_patch)))

    # Every hunk verified before anything is written: a patch applies wholly or
    # not at all, so a failure halfway through cannot leave a half-edited tree.
    changed: list[str] = []
    for logical, target, content in staged:
        if content is None:
            os.remove(target)
        else:
            os.makedirs(os.path.dirname(target) or root_real, exist_ok=True)
            write_text_exact(target, content)
        changed.append(logical)
    return changed


def _resolve_within(root_real: str, relative: str) -> str:
    if os.path.isabs(relative):
        raise PatchError(f"absolute path in patch: {relative!r}")
    candidate = os.path.realpath(os.path.join(root_real, relative))
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        raise PatchError(f"patch path escapes the target root: {relative!r}")
    return candidate


def summarise(patch_text: str) -> dict[str, int]:
    """Line-level shape of a patch: files touched, lines added, lines removed."""
    if not patch_text.strip():
        return {"files": 0, "added": 0, "removed": 0}
    files = parse_patch(patch_text)
    return {
        "files": len(files),
        "added": sum(len(f.added_lines) for f in files),
        "removed": sum(len(f.removed_lines) for f in files),
    }


def iter_paths(patch_text: str) -> Iterable[str]:
    for file_patch in parse_patch(patch_text):
        yield file_patch.path
