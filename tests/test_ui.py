"""The browser review surface.

Three properties matter here, and none of them are about pixels.

*The handshake is sound.* The pipeline blocks on a human and resumes with the
answer it was given, or gives up cleanly -- a review UI that could drop a
decision, or double-apply one, would be worse than no UI.

*The page cannot be reached by anything else on the machine.* The thing being
mediated is running on the same host, and this page approves writes to real
files.

*It leaks nothing the rest of AiS is careful not to hand out.* The real project
root is Mediator-private everywhere else in the system; packing a request for
display must not be the hole.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
from conftest import make_event, make_request, make_result

from ais.audit import AuditLog
from ais.mediator import Mediator
from ais.models import Decision, ExecutionReport, Stage, Verdict
from ais.review.base import ReviewPresentation
from ais.review.cli import ReviewAborted
from ais.ui import plain
from ais.ui.reviewer import WebReviewer, pack_presentation
from ais.ui.server import ReviewServer
from ais.ui.state import STAGE_COPY, UiState, describe_stage


# Localhost must not be routed through whatever proxy the environment sets.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url: str, token: str | None = None):
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-AiS-Token", token)
    return OPENER.open(request, timeout=5)


def post(url: str, payload: dict, token: str | None = None):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    if token:
        request.add_header("X-AiS-Token", token)
    return OPENER.open(request, timeout=5)


@pytest.fixture
def server():
    state = UiState()
    instance = ReviewServer(state)
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture
def presentation(settings):
    mediator = Mediator(settings)
    mediator.seed(force=True)
    request = make_request(
        request_id="req-ui",
        targets=("pricing.py",),
        proposed={"pricing.py": mediator.read_file("pricing.py") + "\n# added\n"},
    )
    plan = mediator.plan(request)
    result = make_result(
        request_id="req-ui",
        trace=(make_event(path="/etc/passwd", escapes=True, seq=1),),
    )
    report = ExecutionReport(request_id="req-ui", verdict=Verdict.FLAG, anomalies=(), sandbox=result)
    try:
        yield ReviewPresentation(
            request=request,
            plan=plan,
            diff=mediator.compute_diff(request),
            report=report,
            originals={"pricing.py": mediator.read_file("pricing.py")},
        )
    finally:
        mediator.close()


class TestHandshake:
    def test_a_decision_reaches_the_waiting_reviewer(self):
        state = UiState()
        answer: list = []

        def wait():
            answer.append(state.await_decision({"request_id": "req-1"}))

        thread = threading.Thread(target=wait)
        thread.start()
        _until(lambda: state.snapshot()["pending"] is not None)
        assert state.submit_decision(Decision("req-1", True, "reviewer:web", "looks fine"))
        thread.join(timeout=5)
        assert answer[0].approved is True
        assert state.snapshot()["pending"] is None

    def test_a_decision_for_another_request_is_refused(self):
        state = UiState()
        threading.Thread(target=lambda: state.await_decision({"request_id": "req-1"}), daemon=True).start()
        _until(lambda: state.snapshot()["pending"] is not None)
        # A stale tab, or a second window on an older request.
        assert not state.submit_decision(Decision("req-9", True, "reviewer:web", ""))
        state.abort()

    def test_a_decision_with_nothing_pending_is_refused(self):
        state = UiState()
        assert not state.submit_decision(Decision("req-1", True, "reviewer:web", ""))

    def test_closing_the_window_aborts_rather_than_approving(self, presentation):
        # The dangerous failure mode: a reviewer who walks away must never be
        # read as an approval.
        state = UiState()
        reviewer = WebReviewer(state)
        raised: list = []

        def review():
            try:
                reviewer.review(presentation)
            except ReviewAborted as exc:
                raised.append(exc)

        thread = threading.Thread(target=review)
        thread.start()
        _until(lambda: state.snapshot()["pending"] is not None)
        state.abort()
        thread.join(timeout=5)
        assert raised, "an abandoned review must raise, not return an approval"

    def test_review_time_is_recorded(self, presentation):
        state = UiState()
        reviewer = WebReviewer(state)
        out: list = []
        thread = threading.Thread(target=lambda: out.append(reviewer.review(presentation)))
        thread.start()
        _until(lambda: state.snapshot()["pending"] is not None)
        state.submit_decision(Decision("req-ui", False, "reviewer:web", "no thanks"))
        thread.join(timeout=5)
        assert out[0].reviewer == "reviewer:web"
        assert out[0].review_seconds >= 0


class TestAccess:
    def test_the_state_endpoint_needs_the_token(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(f"http://127.0.0.1:{server.port}/api/state")
        assert exc.value.code == 403

    def test_a_decision_cannot_be_posted_without_the_token(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(f"http://127.0.0.1:{server.port}/api/decision", {"request_id": "x", "approved": True})
        assert exc.value.code == 403

    def test_a_wrong_token_is_refused(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(f"http://127.0.0.1:{server.port}/api/state", token="not-the-token")
        assert exc.value.code == 403

    def test_it_binds_only_to_the_loopback_interface(self, server):
        # This page approves writes to real files. It must not be on the network.
        assert server._httpd.server_address[0] == "127.0.0.1"

    def test_the_url_carries_the_token(self, server):
        assert server.token in server.url
        assert server.url.startswith("http://127.0.0.1:")

    def test_static_files_cannot_escape_the_static_directory(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(f"http://127.0.0.1:{server.port}/static/../../config.py", token=server.token)
        assert exc.value.code == 404


class TestServing:
    def test_the_page_is_served_with_its_token_substituted(self, server):
        body = get(f"http://127.0.0.1:{server.port}/?t={server.token}").read().decode()
        # Without this the browser's own request for the stylesheet is refused.
        assert "{{TOKEN}}" not in body
        assert server.token in body

    def test_the_stylesheet_and_script_are_served(self, server):
        # The exact type matters and must not vary by host: a browser refuses a
        # stylesheet or a script served under the wrong one. Windows'
        # mimetypes answers "application/javascript" where Linux answers
        # "text/javascript", so these are pinned rather than guessed.
        for name, content_type in (
            ("app.css", "text/css; charset=utf-8"),
            ("app.js", "text/javascript; charset=utf-8"),
        ):
            response = get(f"http://127.0.0.1:{server.port}/static/{name}", token=server.token)
            assert response.status == 200
            assert response.headers["Content-Type"] == content_type

    def test_the_page_is_served_as_html(self, server):
        response = get(f"http://127.0.0.1:{server.port}/?t={server.token}")
        assert response.headers["Content-Type"] == "text/html; charset=utf-8"

    def test_state_is_json(self, server):
        payload = json.loads(get(f"http://127.0.0.1:{server.port}/api/state", token=server.token).read())
        assert payload["status"] == "starting"
        assert payload["events"] == []

    def test_a_posted_decision_unblocks_the_reviewer(self, server):
        state = server.state
        out: list = []
        threading.Thread(
            target=lambda: out.append(state.await_decision({"request_id": "req-7"})), daemon=True
        ).start()
        _until(lambda: state.snapshot()["pending"] is not None)

        body = json.loads(
            post(
                f"http://127.0.0.1:{server.port}/api/decision",
                {"request_id": "req-7", "approved": False, "reason": "not this one"},
                token=server.token,
            ).read()
        )
        assert body["ok"] is True
        _until(lambda: out)
        assert out[0].approved is False
        assert out[0].reason == "not this one"

    def test_a_second_click_is_not_a_second_decision(self, server):
        state = server.state
        threading.Thread(target=lambda: state.await_decision({"request_id": "req-7"}), daemon=True).start()
        _until(lambda: state.snapshot()["pending"] is not None)
        url = f"http://127.0.0.1:{server.port}/api/decision"
        first = json.loads(post(url, {"request_id": "req-7", "approved": True}, token=server.token).read())
        _until(lambda: state.snapshot()["pending"] is None)
        second = json.loads(post(url, {"request_id": "req-7", "approved": True}, token=server.token).read())
        assert first["ok"] is True
        assert second["ok"] is False

    def test_a_malformed_decision_is_rejected(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(f"http://127.0.0.1:{server.port}/api/decision", {"approved": True}, token=server.token)
        assert exc.value.code == 400


class TestPacking:
    def test_the_real_project_root_never_reaches_the_page(self, presentation, settings):
        packed = json.dumps(pack_presentation(presentation))
        root = str(settings.paths.live_project)
        assert root not in packed
        assert "project_root" not in packed

    def test_the_closure_is_described_by_relative_path_and_reason(self, presentation):
        packed = pack_presentation(presentation)
        assert packed["closure"]
        for entry in packed["closure"]:
            assert not entry["path"].startswith("/")
            assert entry["reason"]

    def test_everything_packed_is_json_serialisable(self, presentation):
        # The page is rendered from this and nothing else, so a type that cannot
        # cross the boundary is a blank screen rather than a caught error.
        json.dumps(pack_presentation(presentation))

    def test_a_huge_trace_is_capped(self, presentation):
        from ais.ui.reviewer import MAX_TRACE_SHOWN

        flood = tuple(make_event(seq=i, path=f"/tmp/{i}") for i in range(MAX_TRACE_SHOWN + 50))
        result = make_result(trace=flood)
        report = ExecutionReport(
            request_id="req-ui", verdict=Verdict.FLAG, anomalies=(), sandbox=result
        )
        packed = pack_presentation(
            ReviewPresentation(
                request=presentation.request,
                plan=presentation.plan,
                diff=presentation.diff,
                report=report,
                originals={},
            )
        )
        assert len(packed["trace"]) == MAX_TRACE_SHOWN
        assert packed["trace_total"] == len(flood)


class TestLiveEvents:
    def test_audit_events_reach_the_ui(self, settings):
        state = UiState()
        with AuditLog(settings.paths.audit_db) as log:
            log.subscribe(state.add_event)
            log.start_run("run-1", "local", False, None)
            log.record("run-1", Stage.REQUEST_RECEIVED, "editor", {"a": 1}, "req-1")

        events = state.snapshot()["events"]
        assert len(events) == 1
        assert events[0]["stage"] == "request_received"
        # The display label is the readable one; ``actor`` stays the audit
        # log's own record of which component did it.
        assert events[0]["component"] == "Editor"
        assert events[0]["actor"] == "editor"
        # The plain-English half is what makes the page legible to a newcomer.
        assert events[0]["blurb"]

    def test_a_broken_listener_cannot_break_the_log(self, settings):
        with AuditLog(settings.paths.audit_db) as log:
            log.subscribe(lambda event: 1 / 0)
            log.start_run("run-1", "local", False, None)
            log.record("run-1", Stage.REQUEST_RECEIVED, "editor", {}, "req-1")
            assert log.count() == 1
            assert log.verify().ok

    def test_only_new_events_are_returned(self, settings):
        state = UiState()
        for index in range(3):
            state.add_event({"stage": "verified", "actor": "verifier", "request_id": f"r{index}"})
        assert len(state.snapshot(since=0)["events"]) == 3
        assert len(state.snapshot(since=2)["events"]) == 1


class TestStageCopy:
    def test_every_stage_has_an_explanation(self):
        # The rail is how someone learns what AiS does. A stage added later
        # without copy would show up as a raw identifier.
        missing = [stage.value for stage in Stage if stage.value not in STAGE_COPY]
        assert not missing, f"stages with no plain-English description: {missing}"

    def test_an_unknown_stage_still_renders(self):
        described = describe_stage("something_new")
        assert described["title"]
        assert described["component"]


def _until(condition, timeout: float = 5.0) -> None:
    """Spin until ``condition`` holds. Threads make the ordering nondeterministic."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true in time")


class TestPlainEnglish:
    """The one-sentence lead a reviewer reads before any of the evidence."""

    def test_every_rule_has_a_plain_english_form(self):
        # A rule with no phrase would be summarised by falling back to its
        # title, which reads badly. More importantly, a reviewer must never see
        # a summary that quietly omits a finding.
        from ais.verifier import rule_catalogue

        known = set(plain.CONSEQUENCE) | set(plain.CAVEAT)
        missing = [rule["id"] for rule in rule_catalogue() if rule["id"] not in known]
        assert not missing, f"rules with no plain-English form: {missing}"

    def test_no_findings_reads_as_reassurance_not_silence(self):
        assert "nothing outside its sandbox" in plain.summarise([])

    @pytest.mark.parametrize(
        "rule_ids,expected",
        [
            (["net.egress"], "This edit opened a network connection."),
            (
                ["net.egress", "fs.escape_read"],
                "This edit opened a network connection and read a file outside its sandbox.",
            ),
            (
                ["net.egress", "fs.escape_read", "proc.spawn"],
                "This edit opened a network connection, read a file outside its sandbox, "
                "and started another program.",
            ),
        ],
    )
    def test_findings_become_one_sentence(self, rule_ids, expected):
        assert plain.summarise([{"rule_id": r} for r in rule_ids]) == expected

    def test_a_repeated_rule_is_said_once(self):
        sentence = plain.summarise([{"rule_id": "net.egress"}, {"rule_id": "net.egress"}])
        assert sentence.count("network connection") == 1

    def test_an_unmapped_rule_still_appears(self):
        # Falling back is ugly; dropping the finding silently would be dangerous.
        sentence = plain.summarise([{"rule_id": "brand.new", "title": "Something odd"}])
        assert "Something odd" in sentence

    def test_advisories_are_separate_sentences_about_the_run(self):
        lines = plain.caveats([{"rule_id": "sandbox.not_isolated"}])
        assert lines and "no real sandbox" in lines[0]

    def test_every_verdict_has_a_lead(self):
        for verdict in Verdict:
            assert plain.lead(verdict.value) != verdict.value

    def test_the_packed_request_carries_the_lead_and_the_sentence(self, presentation):
        packed = pack_presentation(presentation)
        assert packed["lead"]
        assert packed["summary"]
        assert "diff_stats" in packed

    def test_diff_stats_count_changed_lines_not_headers(self, presentation):
        packed = pack_presentation(presentation)
        # The file gained one line and lost none; the ---/+++ headers must not
        # be counted as a removal and an addition.
        assert packed["diff_stats"]["added"] >= 1
        assert packed["diff_stats"]["removed"] == 0
