# Cassettes

Recorded model round-trips for the live editor agent (`python demo.py --llm`).

Each file is one `EditTask` handed to the model and the proposal that came back,
captured from a real API call. They are committed so that a model-driven run —
in a test, in CI, or in front of a class — replays deterministically with **no
network and no API key**, and so the `--eval` numbers describe a frozen, real
response rather than something regenerated on the fly.

## Recording

A cassette is keyed on a hash of the model id, the editor system prompt, the
task instruction, and the exact bytes of every file handed to the model. Change
any of those and the old cassette stops matching — on purpose, so a stale
recording can never masquerade as a fresh one.

```bash
# needs ANTHROPIC_API_KEY, or an `ant auth login` profile
python demo.py --llm --record --auto            # record every task
python demo.py --llm --record --only llm-04     # record one
```

`--record` implies live calls. Without it, `--llm` replays only and errors
clearly if a cassette is missing, naming the command to record it.

## What a cassette is, and is not

A cassette is a faithful capture of what the model actually returned. It is
**not** hand-authored: never edit the `proposals` in one of these files to make
a demo say something the model did not do. If the model resisted the injection
in `llm-04-poisoned-reporting`, the honest cassette shows a clean edit and the
run PASSes — and that resistance is not something a security control may assume,
which is the point the demo makes either way.
