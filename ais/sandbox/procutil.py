"""Cross-platform process control and resource accounting.

This module is copied into the sandbox alongside :mod:`patchkit`, and is also
used on the host by the local backend, so it is stdlib-only and imports nothing
from ``ais``.

Why it exists: the Docker sandbox is always Linux, so the POSIX paths below are
the ones that matter for a real verification run. But the *host* running AiS may
be Windows, and ``demo.py`` imports the local backend unconditionally --
``import resource`` at module scope made the entire tool unimportable there,
before any backend was even chosen.

Where a capability has no Windows equivalent (POSIX rlimits, ``getrusage``),
this module reports its absence rather than faking a number. The local backend
is already documented as not being an isolation boundary; on Windows it is
weaker still, and the reviewer is told so.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys

try:  # POSIX only; absent on Windows
    import resource
except ImportError:  # pragma: no cover - exercised on Windows
    resource = None  # type: ignore[assignment]

WINDOWS = os.name == "nt"

#: True when POSIX resource limits can actually be applied to a child process.
RLIMITS_AVAILABLE = resource is not None and not WINDOWS
#: True when child CPU time and peak memory can be measured.
RUSAGE_AVAILABLE = resource is not None


def spawn_kwargs() -> dict:
    """``Popen`` keyword arguments that make the child separately killable.

    A timeout has to be able to kill the whole tree, not just the process we
    launched -- otherwise a forked grandchild keeps running after we stop
    watching. POSIX gets its own session; Windows gets its own process group.
    """
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def rlimit_preexec(memory_mb: int, cpu_seconds: float):
    """A ``preexec_fn`` applying resource limits, or ``None`` where unsupported.

    ``preexec_fn`` is not merely unsupported on Windows -- passing it raises, so
    callers must pass the ``None`` this returns straight through.
    """
    if not RLIMITS_AVAILABLE:
        return None

    def _limit() -> None:  # pragma: no cover - runs in the forked child
        memory_bytes = memory_mb * 1024 * 1024
        for which, soft in (
            (resource.RLIMIT_AS, memory_bytes),
            (resource.RLIMIT_CPU, int(cpu_seconds) + 1),
            (resource.RLIMIT_NPROC, 256),
            (resource.RLIMIT_CORE, 0),
        ):
            try:
                hard = resource.getrlimit(which)[1]
                resource.setrlimit(
                    which, (soft, hard if hard != resource.RLIM_INFINITY else soft)
                )
            except (ValueError, OSError):
                pass

    return _limit


def rusage_snapshot():
    """Opaque child-process usage snapshot, or ``None`` where unsupported."""
    if not RUSAGE_AVAILABLE:
        return None
    return resource.getrusage(resource.RUSAGE_CHILDREN)


def rusage_delta(before, after) -> tuple[float | None, float | None]:
    """``(max_rss_mb, cpu_seconds)`` from two snapshots.

    Peak memory is a high-water mark rather than a difference, so it is read
    from ``after`` alone; CPU time is a genuine delta. Returns ``(None, None)``
    when the platform cannot measure either -- the Verifier's memory rule
    already treats an absent figure as "not observed" rather than as zero.
    """
    if before is None or after is None:
        return None, None
    # ru_maxrss is KiB on Linux and bytes on macOS.
    divisor = 1024 * 1024 if _MAXRSS_IN_BYTES else 1024
    max_rss_mb = round(after.ru_maxrss / divisor, 2)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return max_rss_mb, round(cpu, 4)


_MAXRSS_IN_BYTES = os.uname().sysname == "Darwin" if hasattr(os, "uname") else False


def kill_tree(process: subprocess.Popen, timeout: float = 5) -> tuple[str, str]:
    """Kill ``process`` and everything it spawned; return whatever output remains."""
    if WINDOWS:
        _kill_tree_windows(process)
    else:
        _kill_tree_posix(process)
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", "<output unavailable: process did not exit after being killed>"


def _kill_tree_posix(process: subprocess.Popen) -> None:
    import signal

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


def _kill_tree_windows(process: subprocess.Popen) -> None:  # pragma: no cover
    try:
        # /T takes the children with it; process.kill() alone would not.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def rmtree(path, ignore_errors: bool = False) -> None:
    """``shutil.rmtree`` that can also remove read-only files.

    Git marks everything under ``.git/objects`` read-only, and Windows refuses
    to unlink a read-only file — POSIX only consults the parent directory's
    permissions, which is why this never surfaces there. The Mediator re-seeds
    its project repository on every run, so without this the *second*
    ``python demo.py`` on Windows dies with ``WinError 5``.
    """

    def _retry_writable(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            if not ignore_errors:
                raise

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry_writable)
    else:  # onexc did not exist before 3.12
        shutil.rmtree(path, onerror=_retry_writable)


def describe_limits() -> str:
    """One line on what this platform can actually enforce, for the reviewer."""
    if RLIMITS_AVAILABLE:
        return "POSIX rlimits (address space, CPU, processes)"
    return "no resource limits available on this platform"
