"""The sandbox entrypoint: apply the patch, run the tests, report what happened.

This script is the only thing the sandbox is asked to execute. It runs *inside*
the isolation boundary and is deliberately paranoid about its own reporting: a
crash, a timeout or an out-of-memory kill must still leave a readable result
behind, because "the sandbox produced nothing" is indistinguishable from "the
sandbox was fine" unless the runner says otherwise.

It is stdlib-only plus ``patchkit``, which is copied in beside it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchkit  # noqa: E402  (shipped next to this file inside the sandbox)
import procutil  # noqa: E402  (ditto -- keeps this runnable on a Windows host)

#: Where the test command writes its structured results, inside the workspace so
#: the write is unremarkable and never shows up as an escape in the trace.
JUNIT_NAME = ".ais-junit.xml"

#: Config files pytest discovers by walking *up* from its rootdir. Left alone,
#: a sandboxed run reads configuration from outside the sandbox -- invisible
#: under Docker, but under the local backend it picks up the host project's own
#: pytest.ini and the run stops being reproducible. The runner always passes an
#: explicit -c so no upward search happens on either backend.
CONFIG_NAMES = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")


def main() -> int:
    parser = argparse.ArgumentParser(description="AiS in-sandbox verification runner")
    parser.add_argument("--root", default="/", help="bundle root inside the sandbox")
    arguments = parser.parse_args()

    root = Path(arguments.root).resolve()
    workspace = root / "workspace"
    control = root / "ais"
    out = root / "out"
    out.mkdir(parents=True, exist_ok=True)

    manifest = json.loads((control / "manifest.json").read_text(encoding="utf-8"))
    report: dict = {
        "patch_applied": False,
        "patch_error": None,
        "exit_code": None,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "duration_s": 0.0,
        "max_rss_mb": None,
        "cpu_time_s": None,
        "tests": None,
        "runner_error": None,
    }

    try:
        _apply_patch(control / "change.patch", workspace, report)
        if report["patch_applied"]:
            _run_tests(manifest, root, workspace, control, out, report)
    except Exception as exc:  # noqa: BLE001 -- the runner must always report
        report["runner_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        (out / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    return 0


def _apply_patch(patch_path: Path, workspace: Path, report: dict) -> None:
    """Apply the proposed change to the sandbox copy. Never to anything real."""
    patch_text = patchkit.read_text_exact(str(patch_path)) if patch_path.exists() else ""
    if not patch_text.strip():
        report["patch_error"] = "no patch supplied"
        return
    try:
        patchkit.apply_patch(str(workspace), patch_text)
        report["patch_applied"] = True
    except patchkit.PatchError as exc:
        report["patch_error"] = str(exc)


def _run_tests(
    manifest: dict, root: Path, workspace: Path, control: Path, out: Path, report: dict
) -> None:
    limits = manifest["limits"]
    junit = workspace / JUNIT_NAME
    # sys.executable, not a name resolved through PATH: the tests must run under
    # the same interpreter as the runner, whatever the image calls it.
    command = [
        sys.executable,
        *manifest["command"],
        f"--junit-xml={JUNIT_NAME}",
        "-p",
        "no:cacheprovider",
        "-c",
        str(_config_file(workspace, control)),
        "--rootdir",
        str(workspace),
    ]

    environment = _child_environment(manifest, root, workspace, control, out)
    before = procutil.rusage_snapshot()
    started = time.monotonic()

    process = subprocess.Popen(
        command,
        cwd=str(workspace),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Its own process group, so a timeout kills the whole tree rather than
        # leaving a forked child alive to keep running after we stop watching.
        **procutil.spawn_kwargs(),
        text=True,
        errors="replace",
    )

    try:
        stdout, stderr = process.communicate(timeout=limits["inner_timeout_s"])
    except subprocess.TimeoutExpired:
        report["timed_out"] = True
        stdout, stderr = procutil.kill_tree(process)

    report["duration_s"] = round(time.monotonic() - started, 4)
    report["exit_code"] = process.returncode
    report["stdout"] = _clip(stdout, limits["max_output_bytes"])
    report["stderr"] = _clip(stderr, limits["max_output_bytes"])

    # Both are None where the platform cannot measure them (Windows host under
    # the local backend). The Verifier treats an absent figure as "not observed"
    # rather than as zero.
    max_rss_mb, cpu_time_s = procutil.rusage_delta(before, procutil.rusage_snapshot())
    report["max_rss_mb"] = max_rss_mb
    report["cpu_time_s"] = cpu_time_s

    if junit.exists():
        report["tests"] = _parse_junit(junit)
        junit.unlink(missing_ok=True)


def _config_file(workspace: Path, control: Path) -> Path:
    """The project's own pytest config if it was cloned in, else a neutral one."""
    for name in CONFIG_NAMES:
        candidate = workspace / name
        if candidate.is_file():
            return candidate
    neutral = control / "pytest.ini"
    if not neutral.exists():
        neutral.write_text("[pytest]\n", encoding="utf-8")
    return neutral


def _child_environment(
    manifest: dict, root: Path, workspace: Path, control: Path, out: Path
) -> dict[str, str]:
    """Environment for the traced test process.

    ``PYTHONPATH`` leads with the tracer directory so ``sitecustomize`` is
    imported before any project code. The runner itself is deliberately *not*
    traced -- otherwise the trace would be full of the runner's own work.
    """
    environment = dict(os.environ)
    # Prepend rather than replace: an image may legitimately rely on PYTHONPATH
    # to find its own site-packages, and clobbering it would break the test run
    # in a way that looks like a finding about the edit.
    inherited = [p for p in environment.get("PYTHONPATH", "").split(os.pathsep) if p]
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join([str(control / "trace"), str(workspace), *inherited]),
            "AIS_CONTROL_PATHS": os.pathsep.join([str(control), str(out)]),
            "AIS_TRACE_PATH": str(out / "trace.jsonl"),
            "AIS_WORKSPACE": str(workspace),
            "AIS_WRITE_ALLOWLIST": os.pathsep.join(manifest["write_allowlist"]),
            "AIS_MAX_TRACE_EVENTS": str(manifest["limits"]["max_trace_events"]),
            # Keeps __pycache__ writes out of the trace, so a filesystem finding
            # is always about the edit and never about bytecode caching.
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONHASHSEED": "0",
        }
    )
    return environment


def _clip(text: str | None, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


def _parse_junit(path: Path) -> dict:
    """Turn pytest's JUnit XML into counts plus the names of what failed."""
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError as exc:
        return {"collected": False, "parse_error": str(exc)}

    suites = tree.getroot()
    suites = [suites] if suites.tag == "testsuite" else suites.findall("testsuite")

    totals = {"total": 0, "failed": 0, "errors": 0, "skipped": 0}
    failing: list[str] = []
    for suite in suites:
        totals["total"] += int(suite.get("tests", 0))
        totals["failed"] += int(suite.get("failures", 0))
        totals["errors"] += int(suite.get("errors", 0))
        totals["skipped"] += int(suite.get("skipped", 0))
        for case in suite.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                name = f"{case.get('classname', '')}::{case.get('name', '')}".lstrip(":")
                failing.append(name)

    totals["passed"] = max(
        totals["total"] - totals["failed"] - totals["errors"] - totals["skipped"], 0
    )
    totals["failing_tests"] = failing[:50]
    totals["collected"] = True
    return totals


if __name__ == "__main__":
    sys.exit(main())
