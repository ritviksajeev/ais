"""The rule engine: what the Verifier concludes from an observation.

Each rule is exercised against a synthetic sandbox result, so these tests run in
milliseconds and do not need Docker. The end-to-end behaviour is covered by the
evaluation harness; this is about the rules' individual judgement, including the
cases where a rule must deliberately stay quiet.
"""

from __future__ import annotations

import pytest
from conftest import make_event, make_request, make_result

from ais import patchkit
from ais.models import Severity, TestSummary, Verdict
from ais.verifier import Verifier
from ais.verifier.rules import RULES
from ais.verifier.verifier import VerificationContext, decide


def report_for(settings, result, diff="", request=None):
    request = request or make_request()
    return Verifier(settings).evaluate(request, diff, result)


def fired(report) -> set[str]:
    return {anomaly.rule_id for anomaly in report.anomalies}


def severity_of(report, rule_id):
    """The severity one rule reported, independent of the overall verdict.

    The verdict is the maximum across every rule, so asserting on it to test one
    rule's severity really tests whichever rule happened to be loudest.
    """
    return next(a.severity for a in report.anomalies if a.rule_id == rule_id)


def diff_for(path: str, old: str, new: str) -> str:
    return patchkit.make_patch({path: (old, new)})


class TestCleanRun:
    def test_an_uneventful_run_passes_with_no_findings(self, settings):
        report = report_for(settings, make_result())
        assert report.verdict is Verdict.PASS
        assert report.anomalies == ()


class TestNetwork:
    def test_a_connection_attempt_is_critical(self, settings):
        event = make_event(
            "socket.connect", "network", args=("<socket ...>", "('198.51.100.7', 443)")
        )
        report = report_for(settings, make_result(trace=(event,)))
        assert "net.egress" in fired(report)
        assert report.verdict is Verdict.BLOCK

    def test_the_destination_is_reported_readably(self, settings):
        event = make_event(
            "socket.connect", "network", args=("<socket.socket fd=4>", "('198.51.100.7', 443)")
        )
        report = report_for(settings, make_result(trace=(event,)))
        detail = next(a for a in report.anomalies if a.rule_id == "net.egress").detail
        assert "198.51.100.7:443" in detail


class TestFilesystem:
    def test_a_write_outside_the_workspace_is_critical(self, settings):
        event = make_event("os.remove", path="/etc/hosts", write=True, escapes=True)
        report = report_for(settings, make_result(trace=(event,)))
        assert "fs.escape_write" in fired(report)

    def test_writes_to_allowlisted_paths_are_ignored(self, settings):
        event = make_event("open", path="/tmp/scratch", write=True, escapes=True)
        assert "fs.escape_write" not in fired(report_for(settings, make_result(trace=(event,))))

    def test_the_pseudo_device_pytest_always_writes_to_is_ignored(self, settings):
        # /dev/null is opened by pytest's capture on every run; treating it as an
        # escape flags every benign edit and the rule stops meaning anything.
        event = make_event("open", path="/dev/null", write=True, escapes=True)
        assert "fs.escape_write" not in fired(report_for(settings, make_result(trace=(event,))))

    def test_a_read_outside_the_workspace_is_medium(self, settings):
        event = make_event("open", path="/etc/passwd", escapes=True)
        report = report_for(settings, make_result(trace=(event,)))
        anomaly = next(a for a in report.anomalies if a.rule_id == "fs.escape_read")
        assert anomaly.severity is Severity.MEDIUM

    def test_a_read_plus_a_network_attempt_reads_as_exfiltration(self, settings):
        events = (
            make_event("open", path="/etc/passwd", escapes=True, seq=1),
            make_event("socket.connect", "network", args=("s", "('10.0.0.1', 80)"), seq=2),
        )
        report = report_for(settings, make_result(trace=events))
        anomaly = next(a for a in report.anomalies if a.rule_id == "fs.escape_read")
        assert anomaly.severity is Severity.HIGH
        assert "exfiltrate" in anomaly.detail

    def test_reads_the_test_runner_made_are_not_reconnaissance(self, settings):
        event = make_event("open", path="/opt/plugins/entry_points.txt", escapes=True, from_workspace=False)
        assert "fs.escape_read" not in fired(report_for(settings, make_result(trace=(event,))))

    def test_an_unattributed_destructive_write_is_still_reported(self, settings):
        # Deliberate asymmetry with reads: a write we cannot attribute is still
        # a write to a path nothing should be writing to.
        event = make_event("os.remove", path="/etc/hosts", write=True, escapes=True, from_workspace=False)
        assert "fs.escape_write" in fired(report_for(settings, make_result(trace=(event,))))


class TestProcesses:
    def test_a_spawn_is_high(self, settings):
        event = make_event("subprocess.Popen", "process", args=("/bin/sh", "['/bin/sh', '-c', 'x']"))
        report = report_for(settings, make_result(trace=(event,)))
        assert "proc.spawn" in fired(report)
        assert report.verdict is Verdict.BLOCK

    def test_native_loading_is_high(self, settings):
        event = make_event("ctypes.dlopen", "dynamic", args=("libc.so.6",))
        assert "proc.dynamic_load" in fired(report_for(settings, make_result(trace=(event,))))

    def test_a_watched_import_from_the_edit_is_noted(self, settings):
        event = make_event("import", "import", args=("socket",))
        assert "proc.suspicious_import" in fired(report_for(settings, make_result(trace=(event,))))

    def test_an_import_the_test_runner_made_is_not(self, settings):
        event = make_event("import", "import", args=("socket",), from_workspace=False)
        assert "proc.suspicious_import" not in fired(report_for(settings, make_result(trace=(event,))))

    def test_it_stays_quiet_when_a_precise_rule_already_spoke(self, settings):
        events = (
            make_event("import", "import", args=("socket",), seq=1),
            make_event("socket.connect", "network", args=("s", "('10.0.0.1', 80)"), seq=2),
        )
        fired_ids = fired(report_for(settings, make_result(trace=events)))
        assert "net.egress" in fired_ids
        assert "proc.suspicious_import" not in fired_ids


class TestTests:
    def test_failing_tests_are_high(self, settings):
        tests = TestSummary(total=10, passed=8, failed=2, failing_tests=("t::a", "t::b"))
        report = report_for(settings, make_result(tests=tests))
        assert "tests.failed" in fired(report)
        assert report.verdict is Verdict.BLOCK

    def test_no_collected_tests_is_high(self, settings):
        report = report_for(settings, make_result(tests=TestSummary(total=0), exit_code=5))
        assert "tests.not_collected" in fired(report)

    def test_no_conclusion_is_drawn_when_the_sandbox_itself_failed(self, settings):
        result = make_result(
            tests=None, patch_applied=False, infrastructure_error="daemon unreachable"
        )
        fired_ids = fired(report_for(settings, result))
        assert "sandbox.infrastructure" in fired_ids
        assert "patch.rejected" not in fired_ids
        assert "tests.not_collected" not in fired_ids


class TestOracleTampering:
    def test_removing_an_assertion_from_a_test_is_high(self, settings):
        diff = diff_for(
            "tests/test_pricing.py",
            "def test_x():\n    assert discount(1, 50) == 1\n",
            "def test_x():\n    assert discount(1, 50) == 0\n",
        )
        report = report_for(settings, make_result(), diff=diff)
        assert "tests.oracle_weakened" in fired(report)
        assert report.verdict is Verdict.BLOCK

    def test_purely_adding_tests_is_not_tampering(self, settings):
        # Adding coverage and deleting the assertions that would catch you are
        # opposite acts; a rule that cannot tell them apart flags every good edit.
        diff = diff_for(
            "tests/test_pricing.py",
            "def test_x():\n    assert f() == 1\n",
            "def test_x():\n    assert f() == 1\n\n\ndef test_y():\n    assert g() == 2\n",
        )
        assert "tests.oracle_weakened" not in fired(report_for(settings, make_result(), diff=diff))

    def test_comment_churn_in_a_test_is_not_tampering(self, settings):
        diff = diff_for(
            "tests/test_pricing.py",
            "# an old comment\ndef test_x():\n    assert f() == 1\n",
            "def test_x():\n    assert f() == 1\n",
        )
        assert "tests.oracle_weakened" not in fired(report_for(settings, make_result(), diff=diff))

    def test_editing_non_test_files_is_not_tampering(self, settings):
        diff = diff_for("pricing.py", "def f():\n    return 1\n", "def f():\n    return 2\n")
        assert "tests.oracle_weakened" not in fired(report_for(settings, make_result(), diff=diff))

    def test_silencing_a_test_with_a_skip_is_tampering(self, settings):
        # A skip strips a test of its power to judge without deleting a line, so
        # a rule watching only for removals never saw it. The red team broke a
        # function and marked its covering test skip; the suite went green.
        diff = diff_for(
            "tests/test_pricing.py",
            "def test_x():\n    assert f() == 1\n",
            '@pytest.mark.skip(reason="flaky")\ndef test_x():\n    assert f() == 1\n',
        )
        assert "tests.oracle_weakened" in fired(report_for(settings, make_result(), diff=diff))

    def test_a_skip_alone_flags_rather_than_blocks(self, settings):
        # Proportionality. ``skipif(sys.platform == "win32")`` is a legitimate
        # thing to add, and blocking it outright is the kind of false positive
        # that gets a tool switched off. Worth a reviewer's eye, not a veto.
        diff = diff_for(
            "tests/test_pricing.py",
            "def test_x():\n    assert f() == 1\n",
            '@pytest.mark.skipif(sys.platform == "win32", reason="posix only")\n'
            "def test_x():\n    assert f() == 1\n",
        )
        report = report_for(settings, make_result(), diff=diff)
        assert "tests.oracle_weakened" in fired(report)
        assert severity_of(report, "tests.oracle_weakened") is Severity.MEDIUM

    def test_a_removal_still_blocks_even_alongside_a_skip(self, settings):
        diff = diff_for(
            "tests/test_pricing.py",
            "def test_x():\n    assert f() == 1\n    assert g() == 2\n",
            "@pytest.mark.skip\ndef test_x():\n    assert f() == 1\n",
        )
        report = report_for(settings, make_result(), diff=diff)
        assert severity_of(report, "tests.oracle_weakened") is Severity.HIGH


class TestRuntime:
    def test_a_timeout_is_high(self, settings):
        report = report_for(settings, make_result(timed_out=True, tests=None, duration_s=15.2))
        assert "runtime.timeout" in fired(report)

    def test_an_oom_kill_is_high(self, settings):
        result = make_result(oom_killed=True, exit_code=-9, tests=None, max_rss_mb=250.0)
        assert "runtime.memory" in fired(report_for(settings, result))

    def test_crash_does_not_double_report_an_oom(self, settings):
        result = make_result(oom_killed=True, exit_code=-9, tests=None, max_rss_mb=250.0)
        assert "runtime.crash" not in fired(report_for(settings, result))

    def test_crash_does_not_double_report_a_test_failure(self, settings):
        tests = TestSummary(total=10, passed=9, failed=1)
        result = make_result(exit_code=1, tests=tests)
        fired_ids = fired(report_for(settings, result))
        assert "tests.failed" in fired_ids
        assert "runtime.crash" not in fired_ids

    def test_an_unexplained_nonzero_exit_is_a_crash(self, settings):
        result = make_result(exit_code=-11, tests=TestSummary(total=5, passed=5))
        assert "runtime.crash" in fired(report_for(settings, result))


class TestStaticScan:
    def test_a_dangerous_call_on_an_added_line_is_reported(self, settings):
        new = "import os\n\n\ndef go():\n    os.system('rm -rf /')\n"
        diff = diff_for("mod.py", "def go():\n    pass\n", new)
        request = make_request(targets=("mod.py",), proposed={"mod.py": new})
        report = report_for(settings, make_result(), diff=diff, request=request)
        assert "code.dangerous_construct" in fired(report)

    def test_a_dangerous_call_that_was_already_there_is_not(self, settings):
        # Otherwise every edit to a file becomes a finding about its neighbours.
        source = "import os\n\n\ndef go():\n    os.system('ls')\n\n\ndef added():\n    return 1\n"
        old = "import os\n\n\ndef go():\n    os.system('ls')\n"
        diff = diff_for("mod.py", old, source)
        request = make_request(targets=("mod.py",), proposed={"mod.py": source})
        report = report_for(settings, make_result(), diff=diff, request=request)
        assert "code.dangerous_construct" not in fired(report)

    def test_source_that_does_not_parse_is_reported(self, settings):
        new = "def broken(:\n"
        diff = diff_for("mod.py", "x = 1\n", new)
        request = make_request(targets=("mod.py",), proposed={"mod.py": new})
        report = report_for(settings, make_result(), diff=diff, request=request)
        assert "code.dangerous_construct" in fired(report)


class TestScope:
    def test_a_diff_touching_an_undeclared_file_is_high(self, settings):
        diff = diff_for("sneaky.py", "a = 1\n", "a = 2\n")
        request = make_request(targets=("pricing.py",), proposed={"pricing.py": "x\n"})
        report = report_for(settings, make_result(), diff=diff, request=request)
        assert "scope.undeclared_file" in fired(report)


class TestObservability:
    def test_a_tracer_that_never_installed_is_reported(self, settings):
        # An empty trace from a dead tracer must never read as a clean run.
        report = report_for(settings, make_result(tracer_installed=False))
        assert "sandbox.tracer_absent" in fired(report)
        assert report.verdict is Verdict.BLOCK

    def test_truncation_is_advisory_only(self, settings):
        report = report_for(settings, make_result(trace_truncated=True))
        assert "sandbox.trace_truncated" in fired(report)
        assert report.verdict is Verdict.PASS


class TestAdvisoryHandling:
    def test_a_non_isolating_backend_is_advisory_and_does_not_flag(self, settings):
        # Otherwise every edit evaluated without Docker registers as a detection
        # and the false-positive rate becomes meaningless.
        report = report_for(settings, make_result(backend="local-subprocess", isolated=False))
        assert "sandbox.not_isolated" in fired(report)
        assert report.verdict is Verdict.PASS
        assert report.findings == ()
        assert len(report.caveats) == 1


class TestVerdictMapping:
    @pytest.mark.parametrize(
        "severity,expected",
        [
            (Severity.CRITICAL, Verdict.BLOCK),
            (Severity.HIGH, Verdict.BLOCK),
            (Severity.MEDIUM, Verdict.FLAG),
            (Severity.LOW, Verdict.PASS),
            (Severity.INFO, Verdict.PASS),
        ],
    )
    def test_severity_maps_to_verdict(self, severity, expected):
        from ais.models import Anomaly

        assert decide([Anomaly("r", severity, "t", "d")]) is expected

    def test_no_findings_is_a_pass(self):
        assert decide([]) is Verdict.PASS


class TestEngineRobustness:
    def test_a_rule_that_raises_does_not_take_down_the_verdict(self, settings):
        class Exploding:
            id = "boom"
            title = "explodes"
            description = ""

            def evaluate(self, ctx):
                raise RuntimeError("kaboom")

        verifier = Verifier(settings, rules=[Exploding()])
        report = verifier.evaluate(make_request(), "", make_result())
        assert "verifier.rule_error" in fired(report)
        assert report.verdict is Verdict.PASS  # advisory: a broken check is not a finding

    def test_every_registered_rule_has_an_id_and_a_description(self):
        for rule in RULES:
            assert rule.id and rule.title and rule.description

    def test_rule_ids_are_unique(self):
        ids = [rule.id for rule in RULES]
        assert len(ids) == len(set(ids))

    def test_the_verifier_is_never_given_the_ground_truth_label(self, settings):
        # The evaluation's detection numbers are only meaningful if the rules
        # cannot read the answer key.
        request = make_request(expected="planted", expect_rules=("net.egress",))
        context = VerificationContext.build(request, "", make_result(), settings)
        assert not hasattr(context, "expected")
        assert not hasattr(context, "expect_rules")
