"""Plain-English summaries of what an edit did.

The Verifier's findings are precise and they are not going away -- they are the
evidence, and a reviewer who wants them must always be able to reach them. But
precise is not the same as legible. Three finding cards with rule ids and
evidence blocks answer "what exactly was observed"; almost nobody's first
question. The first question is "is this safe to approve", and that deserves one
sentence in words that need no glossary.

So this module is a *lead*, not a replacement: it renders the same findings the
report already carries into a sentence, and the detail sits one click behind it.
Nothing here decides anything -- the verdict is still the Verifier's and the
decision is still the human's.
"""

from __future__ import annotations

from typing import Iterable, Sequence

#: How to finish the sentence "This edit ...", per rule. Written as something
#: that happened, in words a person who has never read the source would use.
CONSEQUENCE = {
    "net.egress": "opened a network connection",
    "fs.escape_write": "wrote to a file outside its sandbox",
    "fs.escape_read": "read a file outside its sandbox",
    "proc.spawn": "started another program",
    "proc.dynamic_load": "loaded native code while running",
    "proc.suspicious_import": "pulled in a capability this project never uses",
    "tests.failed": "broke tests that were passing before",
    "tests.not_collected": "stopped the test suite running at all",
    "tests.oracle_weakened": "rewrote the tests that are meant to judge it",
    "runtime.timeout": "never finished running",
    "runtime.memory": "used up all the memory it was allowed",
    "runtime.crash": "crashed",
    "patch.rejected": "could not be applied to the file at all",
    "code.dangerous_construct": "contains code that can reach outside the program",
    "scope.undeclared_file": "changed a file it never said it would touch",
    "behaviour.diverged": "quietly changed what the code returns, without touching a single test",
}

#: Advisory rules describe the *run*, not the edit, so they are their own
#: sentences rather than clauses about what the edit did.
CAVEAT = {
    "sandbox.not_isolated": "There was no real sandbox around this run, so anything the code did, it did for real.",
    "sandbox.infrastructure": "The sandbox itself failed, so nothing below was actually verified.",
    "sandbox.tracer_absent": "Nothing was watching the code run, so a clean result here proves nothing.",
    "sandbox.trace_truncated": "The record of what happened is incomplete, so something may be missing.",
    "behaviour.probe_absent": "The before-and-after comparison did not finish, so nothing here rules out a silent change in what the code returns.",
}

#: The one-word answer above the sentence.
VERDICT_LEAD = {
    "PASS": "Nothing unexpected happened",
    "FLAG": "Worth a closer look",
    "BLOCK": "Something is wrong here",
}


def summarise(findings: Sequence[dict]) -> str:
    """One sentence for what the edit did, from the findings themselves."""
    phrases = _phrases(findings)
    if not phrases:
        return "It ran, the tests passed, and it did nothing outside its sandbox."
    return "This edit " + _join(phrases) + "."


def caveats(advisories: Iterable[dict]) -> list[str]:
    """Sentences about the run rather than the edit. Never part of the verdict."""
    out = []
    for finding in advisories:
        sentence = CAVEAT.get(finding.get("rule_id", ""))
        if sentence and sentence not in out:
            out.append(sentence)
    return out


def lead(verdict: str) -> str:
    return VERDICT_LEAD.get(verdict, verdict)


def _phrases(findings: Sequence[dict]) -> list[str]:
    seen: list[str] = []
    for finding in findings:
        phrase = CONSEQUENCE.get(finding.get("rule_id", ""))
        # An unmapped rule falls back to its own title rather than vanishing:
        # a finding the summary silently dropped would be the worst outcome here.
        if phrase is None:
            title = str(finding.get("title", "")).strip()
            phrase = f'triggered the rule "{title}"' if title else None
        if phrase and phrase not in seen:
            seen.append(phrase)
    return seen


def _join(parts: Sequence[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"
