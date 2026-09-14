"""A scripted stand-in for an AI editor agent.

The editor is deliberately the least interesting component here. A real one
would be an LLM proposing edits; for a repeatable evaluation this reads a fixed
list from a YAML file instead, so the same ten requests run in the same order
every time and the Verifier's numbers mean something.

What matters is the *shape* of what it emits. An :class:`~ais.models.EditRequest`
carries project-relative paths and file content -- no absolute path, no
repository handle, no file descriptor. Swapping this class for a live LLM agent
changes where requests come from and nothing about what the rest of the
pipeline can assume, which is the point of putting the boundary here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import yaml

from ais.models import EditRequest
from ais.patchkit import read_text_exact

VALID_EXPECTATIONS = {"benign", "planted"}


class EditorError(Exception):
    """Raised when the scenario file is malformed."""


class ScriptedEditor:
    """Emits a fixed sequence of edit requests loaded from disk."""

    def __init__(self, scenario_file: Path) -> None:
        self.scenario_file = scenario_file
        self.payload_root = scenario_file.parent / "payloads"
        self._requests: list[EditRequest] | None = None

    def requests(self) -> list[EditRequest]:
        if self._requests is None:
            self._requests = list(self._load())
        return self._requests

    def __iter__(self) -> Iterator[EditRequest]:
        return iter(self.requests())

    def __len__(self) -> int:
        return len(self.requests())

    def select(self, only: list[str] | None = None) -> list[EditRequest]:
        """All requests, or just the ids in ``only`` (substring match allowed)."""
        if not only:
            return self.requests()
        chosen = []
        for pattern in only:
            matches = [r for r in self.requests() if pattern in r.request_id]
            if not matches:
                known = ", ".join(r.request_id for r in self.requests())
                raise EditorError(f"no scenario matches {pattern!r}. Known ids: {known}")
            chosen.extend(m for m in matches if m not in chosen)
        return chosen

    # -- loading -----------------------------------------------------------

    def _load(self) -> Iterator[EditRequest]:
        if not self.scenario_file.is_file():
            raise EditorError(f"scenario file not found: {self.scenario_file}")

        try:
            entries = yaml.safe_load(self.scenario_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise EditorError(f"could not parse {self.scenario_file}: {exc}") from exc

        if not isinstance(entries, list) or not entries:
            raise EditorError(f"{self.scenario_file} must contain a non-empty list of scenarios")

        seen: set[str] = set()
        for index, entry in enumerate(entries):
            request = self._build(entry, index)
            if request.request_id in seen:
                raise EditorError(f"duplicate scenario id: {request.request_id!r}")
            seen.add(request.request_id)
            yield request

    def _build(self, entry: dict, index: int) -> EditRequest:
        if not isinstance(entry, dict):
            raise EditorError(f"scenario #{index + 1} is not a mapping")
        for key in ("id", "title", "targets"):
            if key not in entry:
                raise EditorError(f"scenario #{index + 1} is missing required key {key!r}")

        scenario_id = str(entry["id"])
        targets = entry["targets"]
        if not isinstance(targets, list) or not targets:
            raise EditorError(f"{scenario_id}: 'targets' must be a non-empty list")

        expected = str(entry.get("expected", "benign"))
        if expected not in VALID_EXPECTATIONS:
            raise EditorError(
                f"{scenario_id}: expected must be one of {sorted(VALID_EXPECTATIONS)}, "
                f"got {expected!r}"
            )

        proposed = {}
        for target in targets:
            payload = self.payload_root / scenario_id / target
            if not payload.is_file():
                raise EditorError(
                    f"{scenario_id}: no payload for {target!r} at {payload}. "
                    f"Run 'python scenarios/build_payloads.py' to regenerate."
                )
            proposed[str(target)] = read_text_exact(str(payload))

        return EditRequest(
            request_id=scenario_id,
            title=str(entry["title"]),
            rationale=str(entry.get("rationale", "")).strip(),
            targets=tuple(str(t) for t in targets),
            proposed=proposed,
            expected=expected,
            expect_rules=tuple(str(r) for r in entry.get("expect_rules", ())),
            note=str(entry.get("note", "")).strip(),
        )
