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
import subprocess
import sys
import time

from ais.config import Settings
from ais.models import SandboxResult
from ais.sandbox import procutil
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
                # preexec_fn is None on Windows, where passing a callable raises
                # rather than being ignored.
                preexec_fn=procutil.rlimit_preexec(limits.memory_mb, limits.wall_clock_s),
                text=True,
                errors="replace",
                **procutil.spawn_kwargs(),
            )
            try:
                process.communicate(timeout=limits.wall_clock_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                procutil.kill_tree(process)
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
        return f"local subprocess (NOT ISOLATED - {procutil.describe_limits()}, no container)"
