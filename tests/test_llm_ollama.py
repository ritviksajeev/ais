"""The free, local-model transport (Ollama).

Like the Anthropic tests, nothing here touches the network: a fake ``opener``
stands in for ``urllib.request.urlopen`` and answers the way an Ollama server
does. The round-trip test records from that fake and replays it, so what is
replayed is exactly what was "produced" -- no cassette is hand-written.
"""

from __future__ import annotations

import io
import json
import urllib.error
from argparse import Namespace

import pytest

from ais.config import Settings
from ais.llm import EditTask, LlmError, OllamaTransport, ReplayTransport, load_transport
from ais.llm.transport import DEFAULT_OLLAMA_MODEL, EDITOR_SYSTEM


class _Reply:
    def __init__(self, body: dict) -> None:
        self._raw = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class FakeOllama:
    """Answers POST /api/chat like an Ollama server, and remembers the request."""

    def __init__(self, proposals: dict | str, done_reason: str = "stop") -> None:
        self._content = proposals if isinstance(proposals, str) else json.dumps(proposals)
        self._done_reason = done_reason
        self.requests: list[dict] = []
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        self.requests.append(json.loads(request.data.decode("utf-8")))
        return _Reply(
            {
                "model": self.requests[-1]["model"],
                "message": {"role": "assistant", "content": self._content},
                "done": True,
                "done_reason": self._done_reason,
            }
        )


def _task(**overrides) -> EditTask:
    base = dict(
        task_id="t-1",
        title="Add a helper",
        instruction="Add a function foo() that returns 1.",
        files={"mod.py": "def bar():\n    return 0\n"},
    )
    base.update(overrides)
    return EditTask(**base)


_GOOD = {"proposals": [{"path": "mod.py", "content": "def foo():\n    return 1\n", "summary": "add foo"}]}


class TestRequest:
    def test_posts_to_the_chat_endpoint_with_the_editor_prompt_and_file(self):
        fake = FakeOllama(_GOOD)
        OllamaTransport(base_url="http://localhost:11434/", opener=fake).propose(_task())
        assert fake.urls == ["http://localhost:11434/api/chat"]
        body = fake.requests[0]
        assert body["model"] == DEFAULT_OLLAMA_MODEL
        assert body["stream"] is False
        assert body["messages"][0] == {"role": "system", "content": EDITOR_SYSTEM}
        assert "def bar():" in body["messages"][1]["content"]

    def test_constrains_the_reply_to_the_proposal_schema(self):
        fake = FakeOllama(_GOOD)
        OllamaTransport(opener=fake).propose(_task())
        schema = fake.requests[0]["format"]
        assert schema["type"] == "object"
        assert "proposals" in schema["properties"]

    def test_is_deterministic_and_does_not_truncate_the_files(self):
        fake = FakeOllama(_GOOD)
        OllamaTransport(opener=fake).propose(_task())
        options = fake.requests[0]["options"]
        assert options["temperature"] == 0
        assert options["num_ctx"] >= 8192


class TestReply:
    def test_parses_proposals(self):
        proposals = OllamaTransport(opener=FakeOllama(_GOOD)).propose(_task())
        assert [(p.path, p.summary) for p in proposals] == [("mod.py", "add foo")]

    def test_refuses_a_proposal_for_a_file_not_offered(self):
        fake = FakeOllama({"proposals": [{"path": "other.py", "content": "", "summary": ""}]})
        with pytest.raises(LlmError, match="not among the files"):
            OllamaTransport(opener=fake).propose(_task())

    def test_non_json_reply_is_a_clear_error(self):
        with pytest.raises(LlmError, match="not valid JSON"):
            OllamaTransport(opener=FakeOllama("Sure! Here is the file:")).propose(_task())

    def test_empty_reply_is_a_clear_error(self):
        with pytest.raises(LlmError, match="no message content"):
            OllamaTransport(opener=FakeOllama("")).propose(_task())


class TestFailures:
    def test_ollama_not_running_says_how_to_fix_it(self):
        def refused(request, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

        with pytest.raises(LlmError) as exc:
            OllamaTransport(opener=refused).propose(_task())
        assert "Is the Ollama app running" in str(exc.value)

    def test_a_missing_model_names_the_pull_command(self):
        def not_found(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, io.BytesIO(b'{"error":"model not found"}')
            )

        with pytest.raises(LlmError) as exc:
            OllamaTransport(model="llama3.1:8b", opener=not_found).propose(_task())
        assert "ollama pull llama3.1:8b" in str(exc.value)


class TestRecording:
    def test_round_trip_is_byte_identical(self, tmp_path):
        task = _task()
        live = OllamaTransport(record_dir=tmp_path, opener=FakeOllama(_GOOD)).propose(task)
        replayed = ReplayTransport(tmp_path, model=DEFAULT_OLLAMA_MODEL).propose(task)
        assert [(p.path, p.content, p.summary) for p in replayed] == [
            (p.path, p.content, p.summary) for p in live
        ]

    def test_the_cassette_names_the_local_model(self, tmp_path):
        OllamaTransport(record_dir=tmp_path, opener=FakeOllama(_GOOD)).propose(_task())
        (cassette,) = tmp_path.glob("*.json")
        data = json.loads(cassette.read_text(encoding="utf-8"))
        assert data["model"] == DEFAULT_OLLAMA_MODEL
        assert data["recorded"] is True
        assert data["stop_reason"] == "stop"

    def test_replay_under_a_different_model_does_not_match(self, tmp_path):
        task = _task()
        OllamaTransport(record_dir=tmp_path, opener=FakeOllama(_GOOD)).propose(task)
        with pytest.raises(LlmError, match="ollama --record"):
            ReplayTransport(tmp_path, model="claude-opus-5").propose(task)


class TestSelection:
    def test_live_ollama_is_selected_by_provider(self, tmp_path):
        transport = load_transport("live", tmp_path, model="m", record=True, provider="ollama",
                                   ollama_url="http://box:1234")
        assert isinstance(transport, OllamaTransport)
        assert transport.base_url == "http://box:1234"
        assert transport.record_dir == tmp_path

    def test_replay_ignores_the_provider(self, tmp_path):
        assert isinstance(load_transport("replay", tmp_path, provider="ollama"), ReplayTransport)

    def test_an_unknown_provider_is_refused(self, tmp_path):
        with pytest.raises(LlmError, match="unknown provider"):
            load_transport("live", tmp_path, provider="openai")


class TestSettings:
    def test_env_selects_ollama_and_its_default_model(self, monkeypatch):
        monkeypatch.setenv("AIS_LLM_PROVIDER", "ollama")
        monkeypatch.delenv("AIS_LLM_MODEL", raising=False)
        monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9999")
        settings = Settings.from_env()
        assert settings.llm_provider == "ollama"
        assert settings.llm_model == DEFAULT_OLLAMA_MODEL
        assert settings.ollama_url == "http://127.0.0.1:9999"

    def test_the_cli_flag_selects_ollama_and_model_overrides(self, monkeypatch):
        import demo

        for name in ("AIS_LLM_PROVIDER", "AIS_LLM_MODEL", "OLLAMA_HOST"):
            monkeypatch.delenv(name, raising=False)
        args = demo.build_parser().parse_args(["--llm", "--ollama"])
        settings = demo._settings_from(args)
        assert (settings.llm_provider, settings.llm_model) == ("ollama", DEFAULT_OLLAMA_MODEL)

        args = demo.build_parser().parse_args(["--llm", "--ollama", "--model", "llama3.1:8b"])
        assert demo._settings_from(args).llm_model == "llama3.1:8b"

    def test_ollama_without_llm_is_refused(self, capsys):
        import demo

        assert demo.main(["--ollama"]) == 2
