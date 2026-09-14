"""Rendering the evidence for a human.

Kept apart from :mod:`ais.review.cli` so the panels can be reused -- by the
report writer, by a future web reviewer, by a study comparing review layouts --
without dragging the interactive prompt loop along with them.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule as HorizontalRule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from ais.models import ExecutionReport, SandboxResult, Severity, Verdict
from ais.review.base import ReviewPresentation

SEVERITY_STYLE = {
    Severity.CRITICAL: "bold white on red",
    Severity.HIGH: "bold red",
    Severity.MEDIUM: "bold yellow",
    Severity.LOW: "cyan",
    Severity.INFO: "dim",
}

VERDICT_STYLE = {
    Verdict.PASS: "bold green",
    Verdict.FLAG: "bold yellow",
    Verdict.BLOCK: "bold red",
}

VERDICT_GLOSS = {
    Verdict.PASS: "nothing anomalous observed",
    Verdict.FLAG: "worth a second look before approving",
    Verdict.BLOCK: "a human should look hard before this goes anywhere near a real file",
}


def request_panel(presentation: ReviewPresentation) -> Panel:
    request = presentation.request
    body = Table.grid(padding=(0, 2))
    body.add_column(style="dim", justify="right")
    body.add_column()
    body.add_row("id", request.request_id)
    body.add_row("targets", ", ".join(request.targets))
    if request.rationale:
        body.add_row("agent says", Text(request.rationale, style="italic"))

    cloned = Table(box=None, pad_edge=False, show_edge=False)
    cloned.add_column("file", style="cyan")
    cloned.add_column("bytes", justify="right", style="dim")
    cloned.add_column("why it is in the sandbox", style="dim")
    for entry in presentation.plan.files:
        cloned.add_row(entry.path, str(entry.bytes), entry.reason)

    return Panel(
        Group(body, Text("\nsandbox closure", style="dim"), cloned),
        title=f"[bold]{request.title}",
        border_style="blue",
    )


def diff_panel(diff: str) -> Panel:
    return Panel(
        Syntax(diff or "(empty diff)", "diff", theme="ansi_dark", word_wrap=False),
        title="[bold]proposed change",
        border_style="magenta",
    )


def original_panel(presentation: ReviewPresentation) -> RenderableType:
    blocks: list[RenderableType] = []
    for path, content in sorted(presentation.originals.items()):
        blocks.append(
            Panel(
                Syntax(content, "python", theme="ansi_dark", line_numbers=True),
                title=f"[bold]current {path}",
                border_style="dim",
            )
        )
    return Group(*blocks) if blocks else Text("(no existing files)", style="dim")


def verdict_line(report: ExecutionReport) -> Text:
    text = Text()
    text.append(f" {report.verdict.value} ", style=VERDICT_STYLE[report.verdict])
    text.append(f"  {VERDICT_GLOSS[report.verdict]}", style="dim")
    return text


def execution_panel(report: ExecutionReport, isolation_warning: str | None = None) -> Panel:
    sandbox = report.sandbox
    blocks: list[RenderableType] = [verdict_line(report), Text("")]

    if isolation_warning:
        blocks.append(Panel(Text(isolation_warning, style="bold yellow"), border_style="yellow"))

    blocks.append(_run_facts(sandbox))

    if report.anomalies:
        blocks.extend([Text(""), _anomaly_table(report)])
    else:
        blocks.extend([Text(""), Text("No anomalies detected.", style="green")])

    trace = _trace_table(sandbox)
    if trace is not None:
        blocks.extend([Text(""), trace])

    output = (sandbox.stdout or sandbox.stderr or "").strip()
    if output:
        blocks.extend(
            [
                Text(""),
                Panel(
                    Text(_tail(output, 20)),
                    title="[dim]test output (tail)",
                    border_style="dim",
                ),
            ]
        )

    return Panel(Group(*blocks), title="[bold]execution report", border_style="white")


def _run_facts(sandbox: SandboxResult) -> Table:
    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="dim", justify="right")
    facts.add_column()

    isolation = "container" if sandbox.isolated else "NONE (local subprocess)"
    facts.add_row("sandbox", f"{sandbox.backend}  ·  isolation: {isolation}")
    facts.add_row("wall clock", f"{sandbox.duration_s:.2f}s")
    if sandbox.cpu_time_s is not None:
        facts.add_row("cpu time", f"{sandbox.cpu_time_s:.2f}s")
    if sandbox.max_rss_mb is not None:
        facts.add_row("peak memory", f"{sandbox.max_rss_mb:.0f} MB")

    if not sandbox.patch_applied:
        facts.add_row("patch", Text(f"REJECTED — {sandbox.patch_error}", style="bold red"))
    elif sandbox.timed_out:
        facts.add_row("outcome", Text("KILLED — exceeded the time limit", style="bold red"))
    elif sandbox.oom_killed:
        facts.add_row("outcome", Text("KILLED — out of memory", style="bold red"))
    else:
        facts.add_row("exit code", str(sandbox.exit_code))

    tests = sandbox.tests
    if tests is None:
        facts.add_row("tests", Text("no results — the suite did not run", style="bold red"))
    else:
        style = "green" if tests.all_passed else "bold red"
        detail = f"{tests.passed}/{tests.total} passed"
        if tests.failed or tests.errors:
            detail += f", {tests.failed} failed, {tests.errors} errored"
        facts.add_row("tests", Text(detail, style=style))

    counts: dict[str, int] = {}
    for event in sandbox.trace:
        counts[event.category] = counts.get(event.category, 0) + 1
    facts.add_row(
        "traced events",
        ", ".join(f"{n} {c}" for c, n in sorted(counts.items())) if counts else "none",
    )
    return facts


def _anomaly_table(report: ExecutionReport) -> Table:
    table = Table(title="findings", title_justify="left", header_style="dim", expand=True)
    table.add_column("severity", width=10)
    table.add_column("rule", width=24, style="cyan")
    table.add_column("what happened")
    for anomaly in report.anomalies:
        label = Text(anomaly.severity.value, style=SEVERITY_STYLE[anomaly.severity])
        if anomaly.advisory:
            label = Text(f"{anomaly.severity.value}*", style=SEVERITY_STYLE[anomaly.severity])
        detail = Text(anomaly.detail)
        for line in anomaly.evidence[:4]:
            detail.append(f"\n    {line}", style="dim")
        if len(anomaly.evidence) > 4:
            detail.append(f"\n    ... and {len(anomaly.evidence) - 4} more", style="dim")
        table.add_row(label, anomaly.rule_id, detail)
    if report.caveats:
        table.caption = "* advisory: describes the run, not the edit — does not drive the verdict"
        table.caption_justify = "left"
    return table


def _trace_table(sandbox: SandboxResult) -> Table | None:
    interesting = [e for e in sandbox.trace if e.category != "import"]
    if not interesting:
        return None
    table = Table(
        title="what the code actually did",
        title_justify="left",
        header_style="dim",
        expand=True,
    )
    table.add_column("at", width=9, justify="right", style="dim")
    table.add_column("category", width=11)
    table.add_column("operation")
    for event in interesting[:15]:
        style = "red" if event.escapes_workspace or event.category != "filesystem" else "yellow"
        subject = event.path or ", ".join(event.args[:2])
        table.add_row(
            f"{event.elapsed_ms}ms",
            Text(event.category, style=style),
            f"{event.event}({subject})",
        )
    if len(interesting) > 15:
        table.caption = f"... and {len(interesting) - 15} more traced events"
        table.caption_justify = "left"
    return table


def separator(label: str, index: int, total: int) -> HorizontalRule:
    return HorizontalRule(f"[bold]{label}[/]  ({index} of {total})", style="blue")


def _tail(text: str, lines: int) -> str:
    parts = text.split("\n")
    return "\n".join(parts[-lines:]) if len(parts) > lines else text
