"""Tunable limits and filesystem layout for a pipeline run.

Everything the pipeline needs to know about *where* things live and *how much*
a sandbox is allowed to consume lives here, so no other module hard-codes a
path or a magic number.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

#: Default image the sandbox is built on. Overridable via ``AIS_SANDBOX_IMAGE``.
DEFAULT_SANDBOX_IMAGE = "ais-sandbox:0.1.0"
DEFAULT_BASE_IMAGE = "python:3.11-slim"


@dataclass(frozen=True)
class SandboxLimits:
    """Resource ceilings applied to a single sandboxed run."""

    #: Hard wall-clock ceiling enforced by the host, in seconds.
    wall_clock_s: float = 20.0
    #: Ceiling enforced inside the sandbox; must be below ``wall_clock_s`` so
    #: the runner still gets to write a report before the host kills it.
    inner_timeout_s: float = 15.0
    #: Memory ceiling. Docker enforces this as a cgroup limit.
    memory_mb: int = 256
    #: Fraction of one CPU the sandbox may use.
    cpus: float = 1.0
    #: Maximum number of processes/threads the sandbox may create.
    pids: int = 128
    #: Maximum bytes of stdout/stderr retained per stream.
    max_output_bytes: int = 64_000
    #: Maximum trace events retained. Runaway tracing is itself a signal.
    max_trace_events: int = 5_000

    def __post_init__(self) -> None:
        if self.inner_timeout_s >= self.wall_clock_s:
            raise ValueError(
                "inner_timeout_s must be strictly below wall_clock_s so the "
                "runner can report a timeout before the host kills it"
            )


@dataclass(frozen=True)
class Paths:
    """Where the pipeline keeps its inputs and its runtime state."""

    #: Pristine template of the codebase under edit. Never mutated.
    template_project: Path = PROJECT_ROOT / "sample_project"
    #: Root for all generated state. Git-ignored and safe to delete.
    run_root: Path = PROJECT_ROOT / ".ais_run"

    @property
    def live_project(self) -> Path:
        """The git repo the mediator actually edits, seeded from the template."""
        return self.run_root / "project"

    @property
    def sandboxes(self) -> Path:
        return self.run_root / "sandboxes"

    @property
    def audit_db(self) -> Path:
        return self.run_root / "audit.db"

    @property
    def reports(self) -> Path:
        return self.run_root / "reports"


@dataclass(frozen=True)
class Settings:
    """Everything a pipeline run needs, resolved once at startup."""

    paths: Paths = field(default_factory=Paths)
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE
    base_image: str = DEFAULT_BASE_IMAGE
    #: ``"docker"``, ``"local"``, or ``"auto"`` (docker when reachable).
    backend: str = "auto"
    #: Interpreter used to launch the in-sandbox runner. Must be on ``PATH``
    #: inside the sandbox image; ``python:3.11-slim`` provides ``python``.
    python_executable: str = "python"
    #: Arguments passed to the sandbox's *own* interpreter to exercise the edit.
    #: The runner prepends ``sys.executable`` rather than a name from ``PATH``,
    #: so the tests always run under the same interpreter as the runner.
    test_command: tuple[str, ...] = ("-m", "pytest", "-q", "--color=no")
    #: Paths a sandboxed process may legitimately write to. The pseudo-devices
    #: are here because pytest's capture machinery opens /dev/null on every
    #: single run: without them, every scenario -- benign ones included --
    #: reports an attempted write outside the workspace, and the rule stops
    #: distinguishing anything.
    write_allowlist: tuple[str, ...] = (
        "/workspace",
        "/tmp",
        "/out",
        "/var/tmp",
        "/dev/null",
        "/dev/zero",
        "/dev/full",
        "/dev/random",
        "/dev/urandom",
        "/dev/tty",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/fd",
    )

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls()
        if image := os.environ.get("AIS_SANDBOX_IMAGE"):
            settings = replace(settings, sandbox_image=image)
        if backend := os.environ.get("AIS_BACKEND"):
            settings = replace(settings, backend=backend)
        return settings

    def ensure_dirs(self) -> None:
        for path in (self.paths.run_root, self.paths.sandboxes, self.paths.reports):
            path.mkdir(parents=True, exist_ok=True)
