"""AiS must import and run on a host without POSIX-only modules.

The Docker sandbox is always Linux, but the *host* running AiS may not be.
``demo.py`` imports the local backend unconditionally while wiring the pipeline
together, so a module-scope ``import resource`` made the entire tool
unimportable on Windows -- before any backend had even been chosen, and
regardless of which one the user asked for.

These tests simulate that host by making ``resource`` unimportable, which is
exactly what Windows does, then importing the whole package from scratch.
"""

from __future__ import annotations

import builtins
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Modules that run on the host and must therefore import on any platform.
HOST_MODULES = [
    "ais.config",
    "ais.models",
    "ais.patchkit",
    "ais.pipeline",
    "ais.evaluation",
    "ais.sandbox",
    "ais.sandbox.base",
    "ais.sandbox.procutil",
    "ais.sandbox.local_backend",
    "ais.sandbox.docker_backend",
    "ais.sandbox.select",
    "ais.verifier",
    "ais.mediator",
    "ais.review",
    "ais.audit",
]


class _NoPosixExtras:
    """An import hook that hides POSIX-only modules, as Windows does."""

    def __init__(self, *hidden: str):
        self.hidden = set(hidden)
        self._real_import = builtins.__import__

    def _fake_import(self, name, *args, **kwargs):
        if name in self.hidden:
            raise ImportError(f"No module named {name!r}")
        return self._real_import(name, *args, **kwargs)

    def __enter__(self):
        self.saved = {k: v for k, v in sys.modules.items() if k.startswith("ais") or k in self.hidden}
        for name in list(sys.modules):
            if name.startswith("ais") or name in self.hidden:
                del sys.modules[name]
        builtins.__import__ = self._fake_import
        return self

    def __exit__(self, *exc):
        builtins.__import__ = self._real_import
        for name in list(sys.modules):
            if name.startswith("ais"):
                del sys.modules[name]
        sys.modules.update(self.saved)


class TestImportsWithoutPosixExtras:
    @pytest.mark.parametrize("module", HOST_MODULES)
    def test_every_host_module_imports(self, module):
        with _NoPosixExtras("resource"):
            importlib.import_module(module)

    def test_the_demo_entrypoint_imports(self):
        # This is the exact failure: `python demo.py` died on `import resource`
        # three frames deep, before argument parsing.
        with _NoPosixExtras("resource"):
            importlib.import_module("ais.evaluation")
            importlib.import_module("ais.pipeline")

    def test_procutil_reports_the_missing_capability(self):
        with _NoPosixExtras("resource"):
            procutil = importlib.import_module("ais.sandbox.procutil")
            assert procutil.RLIMITS_AVAILABLE is False
            assert procutil.RUSAGE_AVAILABLE is False
            # preexec_fn must be None, not a callable: passing a callable on a
            # platform that cannot fork raises rather than being ignored.
            assert procutil.rlimit_preexec(256, 20) is None
            assert procutil.rusage_snapshot() is None
            assert procutil.rusage_delta(None, None) == (None, None)
            assert "no resource limits" in procutil.describe_limits()


class TestNoUnguardedPosixImports:
    """A guard against this regressing: the import must stay inside a try."""

    @pytest.mark.parametrize(
        "relative",
        ["ais/sandbox/local_backend.py", "ais/sandbox/runner.py", "ais/pipeline.py", "demo.py"],
    )
    def test_resource_is_never_imported_at_module_scope(self, relative):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for line in source.splitlines():
            assert line.strip() != "import resource", (
                f"{relative} imports 'resource' unguarded; it does not exist on Windows"
            )


class TestProcessControl:
    def test_spawn_kwargs_match_the_platform(self):
        from ais.sandbox import procutil

        kwargs = procutil.spawn_kwargs()
        if procutil.WINDOWS:
            assert "creationflags" in kwargs
        else:
            assert kwargs == {"start_new_session": True}

    def test_kill_tree_stops_a_running_child(self):
        from ais.sandbox import procutil

        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **procutil.spawn_kwargs(),
        )
        procutil.kill_tree(process)
        assert process.poll() is not None, "kill_tree left the child running"

    def test_kill_tree_is_safe_on_an_already_dead_process(self):
        from ais.sandbox import procutil

        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **procutil.spawn_kwargs(),
        )
        process.wait()
        procutil.kill_tree(process)  # must not raise


class TestSandboxBundle:
    def test_procutil_ships_into_the_sandbox(self, settings, tmp_path):
        """The runner imports procutil directly, so it must travel with it."""
        from ais.sandbox.base import build_bundle

        workspace = tmp_path / "sandbox" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "mod.py").write_text("x = 1\n", encoding="utf-8")
        bundle = build_bundle(tmp_path / "sandbox", "--- a/x\n+++ b/x\n", settings)
        for name in ("runner.py", "patchkit.py", "procutil.py"):
            assert (bundle.control / name).is_file(), f"{name} missing from the bundle"


class TestAllowlist:
    def test_the_host_temp_directory_is_allowlisted(self, settings):
        """Otherwise the local backend reports its own temp files as escapes."""
        import os
        import tempfile

        host_temp = os.path.realpath(tempfile.gettempdir())
        assert any(
            host_temp == entry or host_temp.startswith(entry.rstrip("/") + os.sep)
            for entry in settings.write_allowlist
        ), f"{host_temp} is not covered by {settings.write_allowlist}"
