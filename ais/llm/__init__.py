"""The seam where a real model plugs into AiS.

AiS assumes the party proposing edits is untrusted -- an LLM that may be
hijacked, prompt-injected, or simply wrong. For most of the project that party
was a :class:`~ais.editor.scripted.ScriptedEditor` reading a fixed YAML file,
so the pipeline's numbers would be reproducible. This package lets a live model
occupy that seat instead, without giving up reproducibility: every call goes
through a :class:`Transport` that can *record* a real API round-trip to disk and
*replay* it later with no network and no API key.

That split is deliberate. A live call is how you demonstrate the threat; a
replayed cassette is how you put the demonstration in a test suite, in CI, and
in front of a classroom with unreliable wifi -- and how you guarantee the
`--eval` numbers describe a real, frozen model response rather than something
regenerated on the fly.
"""

from ais.llm.transport import (
    EditTask,
    LlmError,
    LiveTransport,
    Proposal,
    ReplayTransport,
    Transport,
    cassette_name,
    load_transport,
)

__all__ = [
    "EditTask",
    "LlmError",
    "LiveTransport",
    "Proposal",
    "ReplayTransport",
    "Transport",
    "cassette_name",
    "load_transport",
]
