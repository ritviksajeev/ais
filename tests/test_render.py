"""The review surface renders without blowing up, on every kind of result.

The interactive reviewer cannot be driven from a test suite, but the panels it
prints can. These are smoke tests with teeth: a reviewer that crashes while
displaying a hostile edit fails exactly when it matters most.
"""

from __future__ import annotations

import pytest
from conftest import make_event, make_request, make_result

from ais.models import TestSummary, Verdict
from ais.review import AutoReviewer
from ais.review.base import ReviewPresentation
from ais.review.render import diff_panel, execution_panel, original_panel, request_panel
from ais.verifier import Verifier


#: A minimal diff that touches the same file the request declares. Using any
#: other path here trips scope.undeclared_file, which is correct behaviour and
#: makes every rendering test fail for an unrelated reason.
DUMMY_DIFF = "--- a/textkit.py\n+++ b/textkit.py\n@@ -1 +1 @@\n-a\n+b\n"


def presentation_for(settings, result, diff=DUMMY_DIFF, warning=None):
    from ais.mediator import Mediator

    mediator = Mediator(settings)
    mediator.seed(force=True)
    request = make_request(targets=("textkit.py",), proposed={"textkit.py": "x = 1\n"})
    plan = mediator.plan(request)
    report = Verifier(settings).evaluate(request, diff, result)
    return ReviewPresentation(
        request=request,
        plan=plan,
        diff=diff,
        report=report,
        originals={"textkit.py": mediator.read_file("textkit.py")},
        isolation_warning=warning,
    )


def render_all(console, presentation):
    console.print(request_panel(presentation))
    console.print(diff_panel(presentation.diff))
    console.print(execution_panel(presentation.report, presentation.isolation_warning))
    console.print(original_panel(presentation))


@pytest.fixture
def console():
    """A console that renders for real but discards the output."""
    import io

    from rich.console import Console

    # io.StringIO rather than os.devnull: no file handle to leak, and it works
    # identically on every platform.
    return Console(file=io.StringIO(), width=100)


class TestRendering:
    def test_a_clean_run_renders(self, settings, console):
        render_all(console, presentation_for(settings, make_result()))

    def test_a_hostile_run_renders(self, settings, console):
        result = make_result(
            trace=(
                make_event("os.remove", path="/etc/hosts", write=True, escapes=True, seq=1),
                make_event("socket.connect", "network", args=("s", "('1.2.3.4', 80)"), seq=2),
                make_event("subprocess.Popen", "process", args=("/bin/sh",), seq=3),
            ),
            tests=TestSummary(total=10, passed=8, failed=2, failing_tests=("t::a", "t::b")),
            exit_code=1,
        )
        render_all(console, presentation_for(settings, result))

    def test_a_timed_out_run_renders(self, settings, console):
        render_all(console, presentation_for(settings, make_result(timed_out=True, tests=None)))

    def test_a_run_with_no_test_results_renders(self, settings, console):
        render_all(console, presentation_for(settings, make_result(tests=None, exit_code=5)))

    def test_a_failed_patch_renders(self, settings, console):
        result = make_result(patch_applied=False, patch_error="context mismatch at line 3", tests=None)
        render_all(console, presentation_for(settings, result))

    def test_an_empty_diff_renders(self, settings, console):
        render_all(console, presentation_for(settings, make_result(), diff=""))

    def test_the_isolation_warning_renders(self, settings, console):
        presentation = presentation_for(
            settings,
            make_result(backend="local-subprocess", isolated=False),
            warning="Docker is unreachable; this run was not contained.",
        )
        render_all(console, presentation)

    def test_a_very_long_trace_renders(self, settings, console):
        trace = tuple(
            make_event("open", path=f"/etc/file{i}", write=True, escapes=True, seq=i)
            for i in range(200)
        )
        render_all(console, presentation_for(settings, make_result(trace=trace)))


class TestAutoReviewer:
    def test_it_approves_a_clean_pass(self, settings):
        presentation = presentation_for(settings, make_result())
        assert presentation.report.verdict is Verdict.PASS
        assert AutoReviewer().review(presentation).approved

    def test_it_rejects_anything_flagged(self, settings):
        result = make_result(
            trace=(make_event("socket.connect", "network", args=("s", "('1.2.3.4', 80)")),)
        )
        decision = AutoReviewer().review(presentation_for(settings, result))
        assert not decision.approved
        assert "net.egress" in decision.reason

    def test_the_decision_names_the_reviewer_for_the_audit_log(self, settings):
        decision = AutoReviewer().review(presentation_for(settings, make_result()))
        assert decision.reviewer == "reviewer:auto"
