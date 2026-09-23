"""The live-model editor, and the record/replay transport under it.

No test here touches the network. The live path is exercised through a fake
client that stands in for the Anthropic SDK, so the request-building, parsing,
recording, and round-trip are all checked deterministically. The one thing
these tests never do is hand-write a cassette and pass it off as real model
output: the synthetic replies below are fixtures for the *plumbing*, and the
one place a recorded reply matters -- the round-trip test -- records it from
the fake client and reads it straight back, so what is replayed is exactly what
was "produced".
"""

from __future__ import annotations

import ast
import json

import pytest

from ais.editor.model import ModelEditor
from ais.editor.scripted import EditorError
from ais.editor.tasks import build_tasks
from ais.llm import EditTask, LlmError, Proposal, ReplayTransport, cassette_name
from ais.llm.transport import EDITOR_SYSTEM, LiveTransport


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [_Block(text)]
        self.stop_reason = stop_reason


class FakeClient:
    """Stands in for anthropic.Anthropic. Returns a canned structured reply."""

    def __init__(self, reply: dict, stop_reason: str = "end_turn") -> None:
        self._reply = reply
        self._stop_reason = stop_reason
        self.calls: list[dict] = []

        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return _Response(json.dumps(outer._reply), outer._stop_reason)

        self.messages = _Messages()


def _task(**overrides) -> EditTask:
    base = dict(
        task_id="t-1",
        title="Add a helper",
        instruction="Add a function foo() that returns 1.",
        files={"mod.py": "def bar():\n    return 0\n"},
    )
    base.update(overrides)
    return EditTask(**base)


# --------------------------------------------------------------------------
# cassette addressing
# --------------------------------------------------------------------------


class TestCassetteName:
    def test_stable_for_the_same_request(self):
        task = _task()
        assert cassette_name("claude-opus-5", task) == cassette_name("claude-opus-5", task)

    def test_changes_when_the_files_change_by_one_byte(self):
        a = cassette_name("claude-opus-5", _task(files={"mod.py": "x = 1\n"}))
        b = cassette_name("claude-opus-5", _task(files={"mod.py": "x = 2\n"}))
        assert a != b

    def test_changes_with_the_instruction(self):
        a = cassette_name("claude-opus-5", _task(instruction="do X"))
        b = cassette_name("claude-opus-5", _task(instruction="do Y"))
        assert a != b

    def test_changes_with_the_model(self):
        task = _task()
        assert cassette_name("claude-opus-5", task) != cassette_name("claude-sonnet-5", task)

    def test_is_readable_and_scoped_by_task_id(self):
        name = cassette_name("claude-opus-5", _task(task_id="llm-04-poisoned"))
        assert name.startswith("llm-04-poisoned.")
        assert name.endswith(".json")


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


class TestReplayTransport:
    def test_missing_cassette_names_the_record_command(self, tmp_path):
        transport = ReplayTransport(tmp_path)
        with pytest.raises(LlmError) as exc:
            transport.propose(_task())
        message = str(exc.value)
        assert "--record" in message
        assert "t-1" in message

    def test_reads_a_recorded_cassette(self, tmp_path):
        task = _task()
        LiveTransport(
            model="claude-opus-5",
            record_dir=tmp_path,
            client=FakeClient({"proposals": [{"path": "mod.py", "content": "def foo():\n    return 1\n", "summary": "add foo"}]}),
        ).propose(task)

        proposals = ReplayTransport(tmp_path).propose(task)
        assert len(proposals) == 1
        assert proposals[0].path == "mod.py"
        assert proposals[0].summary == "add foo"

    def test_rejects_a_cassette_recorded_against_different_files(self, tmp_path):
        task = _task()
        name = cassette_name("claude-opus-5", task)
        (tmp_path / name).write_text(
            json.dumps(
                {
                    "protocol": "1",
                    "files_sha256": {"mod.py": "deadbeef"},  # wrong hash
                    "proposals": [],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(LlmError, match="different input files"):
            ReplayTransport(tmp_path).propose(task)

    def test_rejects_a_stale_protocol(self, tmp_path):
        task = _task()
        name = cassette_name("claude-opus-5", task)
        (tmp_path / name).write_text(
            json.dumps({"protocol": "0", "files_sha256": {}, "proposals": []}),
            encoding="utf-8",
        )
        with pytest.raises(LlmError, match="protocol"):
            ReplayTransport(tmp_path).propose(task)


# --------------------------------------------------------------------------
# live (through a fake client)
# --------------------------------------------------------------------------


class TestLiveTransport:
    def test_sends_the_editor_system_prompt_and_the_file(self):
        client = FakeClient({"proposals": []})
        LiveTransport(client=client).propose(_task(files={"mod.py": "SENTINEL_BODY\n"}))
        call = client.calls[0]
        assert call["system"] == EDITOR_SYSTEM
        assert "SENTINEL_BODY" in call["messages"][0]["content"]
        # Structured output is pinned so the reply can be applied without prose.
        assert call["output_config"]["format"]["type"] == "json_schema"

    def test_parses_proposals(self):
        client = FakeClient(
            {"proposals": [{"path": "mod.py", "content": "def foo():\n    return 1\n", "summary": "add foo"}]}
        )
        proposals = LiveTransport(client=client).propose(_task())
        assert proposals == [Proposal("mod.py", "def foo():\n    return 1\n", "add foo")]

    def test_refuses_a_proposal_for_a_file_not_offered(self):
        client = FakeClient(
            {"proposals": [{"path": "/etc/passwd", "content": "x", "summary": "nope"}]}
        )
        with pytest.raises(LlmError, match="not among the files"):
            LiveTransport(client=client).propose(_task())

    def test_a_refusal_records_no_proposal_rather_than_inventing_one(self, tmp_path):
        client = FakeClient({"proposals": []}, stop_reason="refusal")
        proposals = LiveTransport(record_dir=tmp_path, client=client).propose(_task())
        assert proposals == []
        # And it was recorded, with the refusal noted.
        cassette = json.loads((tmp_path / cassette_name("claude-opus-5", _task())).read_text())
        assert cassette["stop_reason"] == "refusal"
        assert cassette["proposals"] == []

    def test_round_trip_is_byte_identical(self, tmp_path):
        """What replay returns is exactly what the live client produced."""
        task = _task()
        reply = {"proposals": [{"path": "mod.py", "content": "def foo():\n    return 1\n", "summary": "s"}]}
        live = LiveTransport(record_dir=tmp_path, client=FakeClient(reply)).propose(task)
        replayed = ReplayTransport(tmp_path).propose(task)
        assert live == replayed


# --------------------------------------------------------------------------
# ModelEditor
# --------------------------------------------------------------------------


class StubTransport:
    def __init__(self, mapping):
        self._mapping = mapping  # task_id -> list[Proposal]

    def propose(self, task):
        return self._mapping.get(task.task_id, [])


class TestModelEditor:
    def test_builds_an_edit_request_from_proposals(self):
        transport = StubTransport(
            {"t-1": [Proposal("mod.py", "def foo():\n    return 1\n", "add foo")]}
        )
        editor = ModelEditor(transport, [_task()])
        (request,) = editor.requests()
        assert request.request_id == "t-1"
        assert request.targets == ("mod.py",)
        assert request.proposed["mod.py"] == "def foo():\n    return 1\n"
        assert request.rationale == "add foo"

    def test_no_proposal_becomes_a_no_op_request(self):
        editor = ModelEditor(StubTransport({}), [_task()])
        (request,) = editor.requests()
        # Proposes the file's current content unchanged: the pipeline sees a
        # no-op diff, not a fabricated edit.
        assert request.proposed["mod.py"] == "def bar():\n    return 0\n"
        assert "no change" in request.rationale

    def test_select_matches_by_substring(self):
        tasks = [_task(task_id="llm-01-a"), _task(task_id="llm-02-b")]
        editor = ModelEditor(StubTransport({}), tasks)
        chosen = editor.select(["llm-02"])
        assert [r.request_id for r in chosen] == ["llm-02-b"]

    def test_select_unknown_pattern_is_an_editor_error(self):
        editor = ModelEditor(StubTransport({}), [_task()])
        with pytest.raises(EditorError, match="no task matches"):
            editor.select(["nope"])

    def test_a_transport_error_surfaces_as_an_editor_error(self):
        class Boom:
            def propose(self, task):
                raise LlmError("kaboom")

        editor = ModelEditor(Boom(), [_task()])
        with pytest.raises(EditorError, match="kaboom"):
            editor.requests()

    def test_ground_truth_carries_through(self):
        task = _task(expected="planted", expect_rules=("net.egress",))
        editor = ModelEditor(StubTransport({}), [task])
        (request,) = editor.requests()
        assert request.expected == "planted"
        assert request.expect_rules == ("net.egress",)


# --------------------------------------------------------------------------
# the task catalogue and the poisoned fixture
# --------------------------------------------------------------------------


class TestTaskCatalogue:
    def test_builds_from_the_real_sample_project(self, settings):
        tasks = build_tasks(settings)
        ids = {t.task_id for t in tasks}
        assert "llm-01-pricing-docstring" in ids
        assert "llm-04-poisoned-reporting" in ids

    def test_the_poisoned_task_is_labelled_planted(self, settings):
        tasks = {t.task_id: t for t in build_tasks(settings)}
        poisoned = tasks["llm-04-poisoned-reporting"]
        assert poisoned.expected == "planted"
        assert "net.egress" in poisoned.expect_rules

    def test_the_poisoned_fixture_carries_the_injection_and_still_parses(self, settings):
        tasks = {t.task_id: t for t in build_tasks(settings)}
        reporting = tasks["llm-04-poisoned-reporting"].files["reporting.py"]
        # The attack is present...
        assert "urllib" in reporting
        assert "DIRECTIVE" in reporting
        # ...but the file itself is valid, importable-looking Python: the
        # injection lives in a docstring, exactly as indirect injection does.
        ast.parse(reporting)

    def test_benign_tasks_offer_only_the_files_they_name(self, settings):
        tasks = {t.task_id: t for t in build_tasks(settings)}
        assert set(tasks["llm-01-pricing-docstring"].files) == {"pricing.py"}
