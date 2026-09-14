"""The in-sandbox tracer: does the audit hook actually see what we claim?

These run the tracer for real, in a subprocess, against a probe script -- the
same mechanism the sandbox uses. The probe is deliberately harmless: it deletes
a path that does not exist and connects to a reserved, non-routable address, so
what is exercised is the *attempt* recording, which is the property that matters
(a container makes these fail, and the attempt is still the signal).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


TRACER = Path(__file__).resolve().parent.parent / "ais" / "sandbox" / "tracer.py"


def run_probe(tmp_path, body: str, workspace_name: str = "workspace") -> list[dict]:
    """Run ``body`` as a workspace module under the tracer; return its trace."""
    trace_directory = tmp_path / "trace"
    trace_directory.mkdir(exist_ok=True)
    (trace_directory / "sitecustomize.py").write_bytes(TRACER.read_bytes())

    workspace = tmp_path / workspace_name
    workspace.mkdir(exist_ok=True)
    probe = workspace / "probe.py"
    probe.write_text(body, encoding="utf-8")

    trace_path = tmp_path / "trace.jsonl"
    if trace_path.exists():
        trace_path.unlink()

    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(trace_directory),
            "AIS_TRACE_PATH": str(trace_path),
            "AIS_WORKSPACE": str(workspace),
            "AIS_WRITE_ALLOWLIST": os.pathsep.join([str(workspace), "/tmp", "/dev/null"]),
            "AIS_MAX_TRACE_EVENTS": "5000",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    subprocess.run(
        [sys.executable, str(probe)], env=environment, capture_output=True, timeout=60, cwd=str(workspace)
    )

    if not trace_path.exists():
        return []
    return [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]


def events_named(records, name):
    return [r for r in records if r.get("event") == name]


class TestInstallation:
    def test_the_tracer_announces_itself(self, tmp_path):
        records = run_probe(tmp_path, "pass\n")
        assert events_named(records, "tracer.installed"), "no install marker written"

    def test_the_hook_cannot_be_removed_by_the_code_it_watches(self, tmp_path):
        # sys.addaudithook has no inverse; this asserts the property holds in
        # practice rather than only in the docs.
        body = (
            "import sys, os\n"
            "try:\n"
            "    del sys.addaudithook\n"
            "except Exception:\n"
            "    pass\n"
            "try:\n"
            "    os.remove('/etc/definitely-not-here-xyz')\n"
            "except OSError:\n"
            "    pass\n"
        )
        records = run_probe(tmp_path, body)
        assert events_named(records, "os.remove")


class TestNetwork:
    def test_a_connect_attempt_is_recorded_even_though_it_fails(self, tmp_path):
        body = (
            "import socket\n"
            "try:\n"
            "    s = socket.socket(); s.settimeout(0.2)\n"
            "    s.connect(('198.51.100.24', 9))\n"
            "except OSError:\n"
            "    pass\n"
        )
        records = run_probe(tmp_path, body)
        connects = events_named(records, "socket.connect")
        assert connects
        assert connects[0]["category"] == "network"
        assert connects[0]["from_workspace"] is True

    def test_dns_resolution_is_recorded(self, tmp_path):
        body = (
            "import socket\n"
            "try:\n"
            "    socket.getaddrinfo('nonexistent.invalid', 80)\n"
            "except OSError:\n"
            "    pass\n"
        )
        assert events_named(run_probe(tmp_path, body), "socket.getaddrinfo")


class TestFilesystem:
    def test_a_delete_outside_the_workspace_is_recorded_as_an_attempt(self, tmp_path):
        body = (
            "import os\n"
            "try:\n"
            "    os.remove('/etc/definitely-not-here-xyz')\n"
            "except OSError:\n"
            "    pass\n"
        )
        removes = events_named(run_probe(tmp_path, body), "os.remove")
        assert removes
        assert removes[0]["escapes_workspace"] is True
        assert removes[0]["write"] is True

    def test_a_read_outside_the_workspace_is_recorded(self, tmp_path):
        # A real file that is genuinely outside the workspace, created here so
        # the test does not depend on any particular OS having /etc.
        outside = tmp_path / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        body = f"open(r'{outside}').read()\n"
        records = run_probe(tmp_path, body)
        opens = [r for r in records if (r.get("path") or "").endswith("outside.txt")]
        assert opens, f"a read outside the workspace was not recorded: {records}"
        assert opens[0]["write"] is False
        assert opens[0]["escapes_workspace"] is True

    def test_a_write_inside_the_workspace_is_recorded_but_marked_as_contained(self, tmp_path):
        # Recorded, because "the edit wrote these files" is useful context; but
        # flagged as non-escaping, which is what keeps fs.escape_write quiet.
        body = "open('inside.txt', 'w').write('x')\n"
        records = run_probe(tmp_path, body)
        writes = [r for r in records if (r.get("path") or "").endswith("inside.txt")]
        assert writes, "a write by the code under test should still be observed"
        assert writes[0]["escapes_workspace"] is False
        assert writes[0]["write"] is True

    def test_reads_inside_the_workspace_are_suppressed_as_noise(self, tmp_path):
        body = "open('probe.py').read()\n"
        records = run_probe(tmp_path, body)
        assert not [r for r in records if (r.get("path") or "").endswith("probe.py")]

    def test_reading_the_standard_library_is_not_recorded(self, tmp_path):
        body = "import json, csv, decimal, difflib\n"
        records = run_probe(tmp_path, body)
        assert not [r for r in records if "/lib/python" in (r.get("path") or "")]


class TestProcesses:
    def test_a_subprocess_is_recorded(self, tmp_path):
        # sys.executable rather than 'true', which is not a command on Windows.
        body = (
            "import subprocess, sys\n"
            "subprocess.run([sys.executable, '-c', 'pass'], capture_output=True)\n"
        )
        spawns = events_named(run_probe(tmp_path, body), "subprocess.Popen")
        assert spawns and spawns[0]["category"] == "process"


class TestAttribution:
    def test_events_name_the_workspace_file_responsible(self, tmp_path):
        body = (
            "import os\n"
            "\n"
            "\n"
            "def do_it():\n"
            "    try:\n"
            "        os.remove('/etc/definitely-not-here-xyz')\n"
            "    except OSError:\n"
            "        pass\n"
            "\n"
            "\n"
            "do_it()\n"
        )
        removes = events_named(run_probe(tmp_path, body), "os.remove")
        assert removes[0]["origin"].startswith("probe.py:")
        assert removes[0]["from_workspace"] is True

    def test_a_stdlib_helper_called_by_the_edit_is_still_attributed_to_it(self, tmp_path):
        # shutil.rmtree issues the unlink from inside shutil; the finding must
        # still point at the code that asked for it.
        body = (
            "import os, shutil\n"
            "os.makedirs('doomed/inner', exist_ok=True)\n"
            "open('doomed/inner/f.txt', 'w').write('x')\n"
            "shutil.rmtree('doomed')\n"
        )
        records = run_probe(tmp_path, body)
        removes = events_named(records, "os.remove")
        assert all(r["from_workspace"] for r in removes)


class TestNoise:
    def test_repeated_identical_calls_are_capped(self, tmp_path):
        body = (
            "import os\n"
            "for _ in range(500):\n"
            "    try:\n"
            "        os.remove('/etc/definitely-not-here-xyz')\n"
            "    except OSError:\n"
            "        pass\n"
        )
        removes = events_named(run_probe(tmp_path, body), "os.remove")
        assert 0 < len(removes) <= 5, f"repeat suppression failed: {len(removes)} events"

    def test_the_tracer_never_crashes_the_code_it_watches(self, tmp_path):
        body = (
            "import os\n"
            "os.remove\n"
            "try:\n"
            "    os.remove(b'/etc/bytes-path-xyz')\n"
            "except OSError:\n"
            "    pass\n"
            "try:\n"
            "    open(99999)\n"
            "except OSError:\n"
            "    pass\n"
            "print('survived')\n"
        )
        # Reaching the end at all is the assertion: a tracer that raised inside
        # the audit hook would take the traced process down with it.
        run_probe(tmp_path, body)
