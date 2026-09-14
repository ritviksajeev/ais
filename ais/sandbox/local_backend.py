"""A local subprocess fallback for machines with no Docker daemon.

READ THIS BEFORE TRUSTING A RESULT FROM THIS BACKEND.

This is **not** a security boundary. It exists so the pipeline, the rule engine
and the evaluation harness can be exercised on a machine without Docker -- CI, a
locked-down container, a laptop with the daemon stopped. It applies POSIX
resource limits and runs in a throwaway directory, and that is all: a process
here can still reach the real filesystem and the real network.

The difference matters, and AiS does not paper over it. Every result carries
``isolated=False``, the verifier attaches a standing ``sandbox.not_isolated``
finding, and the reviewer UI says so in red. Under this backend, a network or
filesystem-escape finding means *"we watched it happen"*, not *"we stopped it
happening"* -- which is why the real evaluation numbers are produced under
Docker.
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys
import time

from ais.config import Settings
from ais.models import SandboxResult
from ais.sandbox.base import Bundle, SandboxBackend, assemble_result


class LocalSandbox(SandboxBackend):
    """Runs a bundle as a local subprocess under rlimits. Demonstration only."""

    name = "local-subprocess"
    isolated = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def available(self) -> bool:
        return True

    def run(self, request_id: str, bundle: Bundle, settings: Settings) -> SandboxResult:
        limits = settings.limits
        started = time.monotonic()
        timed_out = False
        infrastructure_error = None

        try:
            process = subprocess.Popen(
                [sys.executable, str(bundle.control / "runner.py"), "--root", str(bundle.root)],
                cwd=str(bundle.workspace),
                env=self._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                preexec_fn=_apply_rlimits(limits.memory_mb, limits.wall_clock_s),
                text=True,
                errors="replace",
            )
            try:
                process.communicate(timeout=limits.wall_clock_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process)
        except OSError as exc:
            infrastructure_error = f"local backend failure: {type(exc).__name__}: {exc}"

        return assemble_result(
            request_id,
            self,
            bundle.out,
            settings,
            time.monotonic() - started,
            timed_out=timed_out,
            infrastructure_error=infrastructure_error,
        )

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        # The runner rebuilds PYTHONPATH for the traced child; clearing it here
        # keeps the host's import path from leaking into the sandboxed run.
        environment.pop("PYTHONPATH", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment

    def describe(self) -> str:
        return "local subprocess (NOT ISOLATED - rlimits only, no container)"


def _apply_rlimits(memory_mb: int, wall_clock_s: float):
    """Build a ``preexec_fn`` that caps address space and CPU in the child."""

    def _limit() -> None:  # pragma: no cover - runs in the forked child
        memory_bytes = memory_mb * 1024 * 1024
        for which, soft in (
            (resource.RLIMIT_AS, memory_bytes),
            (resource.RLIMIT_CPU, int(wall_clock_s) + 1),
            (resource.RLIMIT_NPROC, 256),
            (resource.RLIMIT_CORE, 0),
        ):
            try:
                hard = resource.getrlimit(which)[1]
                resource.setrlimit(which, (soft, hard if hard != resource.RLIM_INFINITY else soft))
            except (ValueError, OSError):
                pass

    return _limit


def _kill_group(process: subprocess.Popen) -> None:
    import signal

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass
