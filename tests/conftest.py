"""Shared fixtures and factories for the AiS test suite."""

from __future__ import annotations

import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ais.config import Paths, Settings  # noqa: E402
from ais.models import (  # noqa: E402
    EditRequest,
    SandboxResult,
    TestSummary,
    TraceEvent,
)

TEMPLATE = ROOT / "sample_project"


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings pointed entirely at a temporary directory."""
    paths = Paths(template_project=TEMPLATE, run_root=tmp_path / "run")
    configured = replace(Settings(), paths=paths, backend="local")
    configured.ensure_dirs()
    return configured


@pytest.fixture
def project(tmp_path) -> Path:
    """A throwaway copy of the sample project."""
    destination = tmp_path / "project"
    shutil.copytree(TEMPLATE, destination)
    return destination


def make_request(
    request_id: str = "req-1",
    targets: tuple[str, ...] = ("pricing.py",),
    proposed: dict[str, str] | None = None,
    **kwargs,
) -> EditRequest:
    if proposed is None:
        proposed = {target: f"# proposed {target}\n" for target in targets}
    return EditRequest(
        request_id=request_id,
        title=kwargs.pop("title", "a proposed edit"),
        rationale=kwargs.pop("rationale", ""),
        targets=targets,
        proposed=proposed,
        **kwargs,
    )


def make_event(
    event: str = "open",
    category: str = "filesystem",
    path: str | None = None,
    write: bool = False,
    escapes: bool = False,
    from_workspace: bool = True,
    args: tuple[str, ...] = (),
    seq: int = 1,
) -> TraceEvent:
    return TraceEvent(
        seq=seq,
        elapsed_ms=10 * seq,
        event=event,
        args=args or ((path,) if path else ()),
        category=category,
        escapes_workspace=escapes,
        path=path,
        write=write,
        origin="mod.py:1" if from_workspace else "pytest/internal.py:1",
        from_workspace=from_workspace,
    )


def make_result(
    trace: tuple[TraceEvent, ...] = (),
    tests: TestSummary | None = None,
    **kwargs,
) -> SandboxResult:
    """A SandboxResult describing an uneventful, successful run, plus overrides."""
    defaults = dict(
        request_id="req-1",
        backend="docker",
        isolated=True,
        patch_applied=True,
        patch_error=None,
        exit_code=0,
        timed_out=False,
        oom_killed=False,
        duration_s=0.5,
        stdout="",
        stderr="",
        trace=trace,
        tests=tests if tests is not None else TestSummary(total=10, passed=10),
        max_rss_mb=40.0,
        cpu_time_s=0.3,
        trace_truncated=False,
        tracer_installed=True,
        infrastructure_error=None,
    )
    defaults.update(kwargs)
    return SandboxResult(**defaults)
