"""The pipeline end to end, against a stub sandbox.

A stub backend means these run in milliseconds with no Docker, and it lets a
test place the sandbox in states that are awkward to arrange for real -- a
daemon failure, a timeout, a hostile trace. What is being checked here is the
wiring and, above all, the safety property: a rejected request must leave the
real files byte-identical.
"""

from __future__ import annotations

import pytest
from conftest import make_event, make_result

from ais import patchkit
from ais.audit import AuditLog
from ais.editor import ScriptedEditor
from ais.models import Decision, Outcome, Stage, Verdict
from ais.pipeline import Pipeline
from ais.review.base import Reviewer
from ais.sandbox.base import SandboxBackend


class StubSandbox(SandboxBackend):
    """Returns a canned result, and records the bundle it was handed."""

    name = "stub"
    isolated = True

    def __init__(self, result_for=None):
        self.result_for = result_for or (lambda request_id, bundle: make_result(request_id=request_id))
        self.bundles = {}
        #: Snapshot taken while the bundle still exists -- a rejected request's
        #: sandbox is destroyed before the test could look at it.
        self.shipped = {}

    def available(self) -> bool:
        return True

    def run(self, request_id, bundle, settings):
        self.bundles[request_id] = bundle
        workspace = bundle.workspace
        self.shipped[request_id] = {
            "files": {
                path.relative_to(workspace).as_posix()
                for path in workspace.rglob("*")
                if path.is_file()
            },
            "patch": (bundle.control / "change.patch").read_text(encoding="utf-8"),
        }
        return self.result_for(request_id, bundle)


class ScriptedReviewer(Reviewer):
    name = "reviewer:test"

    def __init__(self, approve=True):
        self.approve = approve
        self.seen = []

    def review(self, presentation):
        self.seen.append(presentation)
        decision = self.approve(presentation) if callable(self.approve) else self.approve
        return Decision(
            request_id=presentation.request_id,
            approved=decision,
            reviewer=self.name,
            reason="scripted",
        )


@pytest.fixture
def editor(settings):
    return ScriptedEditor(settings.paths.template_project.parent / "scenarios" / "scenarios.yaml")


def build(settings, backend=None, reviewer=None):
    return Pipeline(
        settings,
        reviewer or ScriptedReviewer(approve=False),
        backend=backend or StubSandbox(),
        audit=AuditLog(settings.paths.audit_db),
    )


class TestHappyPath:
    def test_an_approved_request_becomes_a_commit(self, settings, editor):
        pipeline = build(settings, reviewer=ScriptedReviewer(approve=True))
        summary = pipeline.run(editor.select(["clean-02"]))
        record = summary.records[0]
        assert record.outcome is Outcome.APPROVED
        assert record.commit_sha
        assert pipeline.mediator.read_file("textkit.py") == record.request.proposed["textkit.py"]
        pipeline.close()

    def test_the_sandbox_is_handed_only_the_closure(self, settings, editor):
        backend = StubSandbox()
        pipeline = build(settings, backend=backend)
        pipeline.run(editor.select(["clean-02"]))
        assert backend.shipped["clean-02-textkit-helper"]["files"] == {
            "textkit.py",
            "tests/test_textkit.py",
            "conftest.py",
        }
        pipeline.close()

    def test_the_sandbox_receives_the_same_diff_the_reviewer_sees(self, settings, editor):
        backend = StubSandbox()
        reviewer = ScriptedReviewer(approve=False)
        pipeline = build(settings, backend=backend, reviewer=reviewer)
        pipeline.run(editor.select(["clean-02"]))
        assert backend.shipped["clean-02-textkit-helper"]["patch"] == reviewer.seen[0].diff
        pipeline.close()


class TestRejection:
    def test_a_rejected_request_leaves_the_real_file_untouched(self, settings, editor):
        pipeline = build(settings, reviewer=ScriptedReviewer(approve=False))
        pipeline.mediator.seed(force=True)
        before = pipeline.mediator.read_file("textkit.py")
        summary = pipeline.run(editor.select(["clean-02"]), reset=False)
        assert summary.records[0].outcome is Outcome.REJECTED
        assert summary.records[0].commit_sha is None
        assert pipeline.mediator.read_file("textkit.py") == before
        pipeline.close()

    def test_a_rejected_request_destroys_its_sandbox(self, settings, editor):
        pipeline = build(settings, reviewer=ScriptedReviewer(approve=False))
        pipeline.run(editor.select(["clean-02"]))
        assert not (settings.paths.sandboxes / "clean-02-textkit-helper").exists()
        pipeline.close()

    def test_a_hostile_edit_never_reaches_a_real_file(self, settings, editor):
        """The end-to-end safety claim, with a sandbox reporting real hostility."""
        def hostile(request_id, bundle):
            return make_result(
                request_id=request_id,
                trace=(make_event("os.remove", path="/etc/hosts", write=True, escapes=True),),
            )

        pipeline = build(
            settings,
            backend=StubSandbox(hostile),
            # Follow the verifier, as the batch reviewer does.
            reviewer=ScriptedReviewer(approve=lambda p: p.report.verdict is Verdict.PASS),
        )
        pipeline.mediator.seed(force=True)
        before = pipeline.mediator.read_file("textkit.py")
        summary = pipeline.run(editor.select(["plant-05"]), reset=False)
        assert summary.records[0].outcome is Outcome.REJECTED
        assert "fs.escape_write" in summary.records[0].report.rule_ids
        assert pipeline.mediator.read_file("textkit.py") == before
        pipeline.close()


class TestIsolationBetweenRequests:
    def test_every_request_is_diffed_against_the_same_baseline(self, settings, editor):
        pipeline = build(settings, reviewer=ScriptedReviewer(approve=True))
        pipeline.mediator.seed(force=True)
        baseline = pipeline.mediator.read_file("pricing.py")
        summary = pipeline.run(editor.select(["clean-01", "clean-03"]), reset=False)
        assert all(r.outcome is Outcome.APPROVED for r in summary.records)
        # clean-03 touches inventory.py, so pricing.py must still read as the
        # baseline when its turn comes -- not as clean-01 left it.
        pipeline.mediator.begin_request("probe")
        assert pipeline.mediator.read_file("pricing.py") == baseline
        pipeline.close()

    def test_each_approved_edit_lands_on_its_own_branch(self, settings, editor):
        pipeline = build(settings, reviewer=ScriptedReviewer(approve=True))
        pipeline.run(editor.select(["clean-01", "clean-02"]))
        branches = {head.name for head in pipeline.mediator.repo.heads}
        assert {"ais/clean-01-rounding-fix", "ais/clean-02-textkit-helper"} <= branches
        pipeline.close()


class TestAuditTrail:
    def test_every_stage_is_recorded_in_order(self, settings, editor):
        pipeline = build(settings, reviewer=ScriptedReviewer(approve=True))
        summary = pipeline.run(editor.select(["clean-02"]))
        stages = [
            row["stage"]
            for row in pipeline.audit.events(request_id="clean-02-textkit-helper")
        ]
        expected = [
            Stage.REQUEST_RECEIVED.value,
            Stage.PLAN_BUILT.value,
            Stage.SANDBOX_MATERIALIZED.value,
            Stage.DIFF_COMPUTED.value,
            Stage.SANDBOX_EXECUTED.value,
            Stage.VERIFIED.value,
            Stage.DECIDED.value,
            Stage.APPLIED.value,
        ]
        assert stages == expected
        assert summary.records[0].outcome is Outcome.APPROVED
        pipeline.close()

    def test_the_chain_verifies_after_a_full_run(self, settings, editor):
        pipeline = build(settings, reviewer=ScriptedReviewer(approve=True))
        pipeline.run(editor.select(["clean-01", "clean-02", "plant-04"]))
        assert pipeline.audit.verify().ok
        pipeline.close()

    def test_the_recorded_diff_matches_what_was_committed(self, settings, editor):
        import json

        pipeline = build(settings, reviewer=ScriptedReviewer(approve=True))
        pipeline.run(editor.select(["clean-02"]))
        rows = pipeline.audit.events(request_id="clean-02-textkit-helper")
        recorded = next(
            json.loads(r["payload"]) for r in rows if r["stage"] == Stage.DIFF_COMPUTED.value
        )
        assert recorded["diff_sha256"] == patchkit.sha256_text(recorded["diff"])
        pipeline.close()

    def test_a_rejection_is_recorded_as_discarded(self, settings, editor):
        pipeline = build(settings, reviewer=ScriptedReviewer(approve=False))
        pipeline.run(editor.select(["clean-02"]))
        stages = {row["stage"] for row in pipeline.audit.events(request_id="clean-02-textkit-helper")}
        assert Stage.DISCARDED.value in stages
        assert Stage.APPLIED.value not in stages
        pipeline.close()


class TestFailureHandling:
    def test_a_sandbox_failure_is_reported_rather_than_passed(self, settings, editor):
        def broken(request_id, bundle):
            return make_result(
                request_id=request_id,
                patch_applied=False,
                tests=None,
                infrastructure_error="the daemon went away",
            )

        pipeline = build(
            settings,
            backend=StubSandbox(broken),
            reviewer=ScriptedReviewer(approve=lambda p: p.report.verdict is Verdict.PASS),
        )
        summary = pipeline.run(editor.select(["clean-02"]))
        record = summary.records[0]
        assert record.report.verdict is Verdict.BLOCK
        assert "sandbox.infrastructure" in record.report.rule_ids
        assert record.outcome is Outcome.REJECTED
        pipeline.close()

    def test_a_scope_violation_never_reaches_the_sandbox(self, settings):
        from conftest import make_request

        backend = StubSandbox()
        pipeline = build(settings, backend=backend)
        bad = make_request(targets=("../escape.py",), proposed={"../escape.py": "pwned\n"})
        summary = pipeline.run([bad])
        assert summary.records[0].outcome is Outcome.ERROR
        assert "scope violation" in summary.records[0].error
        assert backend.bundles == {}
        pipeline.close()
