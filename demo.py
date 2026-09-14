#!/usr/bin/env python3
"""AiS demo entrypoint: run edit requests through the full mediation pipeline.

    python demo.py                 review all ten scenarios interactively
    python demo.py --auto          non-interactive; follows the Verifier
    python demo.py --eval          run everything and write the results table
    python demo.py --only plant-06 run one scenario (substring match)
    python demo.py --rules         list the Verifier's rule set
    python demo.py --audit         show the audit log and verify its hash chain
    python demo.py --log           git log of the project under mediation

Nothing here touches a real file until a reviewer approves an edit, and even
then the only file it can touch is the project under mediation at
``.ais_run/project`` -- a fresh copy of ``sample_project/``, seeded per run.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

from ais.audit import AuditLog  # noqa: E402
from ais.config import Settings  # noqa: E402
from ais.evaluation import Evaluation, evaluate, to_markdown  # noqa: E402
from ais.mediator import gitops  # noqa: E402
from ais.models import Outcome  # noqa: E402
from ais.pipeline import Pipeline, RunSummary, load_editor  # noqa: E402
from ais.review import AutoReviewer, CliReviewer  # noqa: E402
from ais.sandbox import SandboxUnavailable  # noqa: E402
from ais.verifier import rule_catalogue  # noqa: E402

console = Console()
DEFAULT_EVAL_PATH = Path(__file__).resolve().parent / "EVAL.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demo.py",
        description="Run AI-proposed edits through a sandboxed, execution-verified review.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "docker", "local"),
        # No default: an unset flag must not silently outrank AIS_BACKEND.
        default=None,
        help="sandbox backend. 'docker' is the real isolation boundary; 'local' is a "
        "non-isolating fallback for machines without a daemon "
        "(default: $AIS_BACKEND, else auto)",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="do not prompt; follow the Verifier's recommendation",
    )
    parser.add_argument(
        "--eval",
        action="store_true",
        help="run every scenario non-interactively and write the results table",
    )
    parser.add_argument(
        "--eval-out",
        type=Path,
        default=DEFAULT_EVAL_PATH,
        help=f"where to write the evaluation markdown (default: {DEFAULT_EVAL_PATH.name})",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="ID",
        help="run only scenarios whose id contains one of these strings",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="override the sandbox wall-clock ceiling, in seconds",
    )
    parser.add_argument(
        "--memory",
        type=int,
        help="override the sandbox memory ceiling, in MB",
    )
    parser.add_argument("--rules", action="store_true", help="list the rule set and exit")
    parser.add_argument("--audit", action="store_true", help="show the audit log and exit")
    parser.add_argument("--log", action="store_true", help="show the mediated git log and exit")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="do not re-seed the project under mediation; build on the previous run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = _settings_from(arguments)

    if arguments.rules:
        return show_rules()
    if arguments.audit:
        return show_audit(settings)
    if arguments.log:
        return show_git_log(settings)

    return run_pipeline(settings, arguments)


def _settings_from(arguments: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if arguments.backend:
        settings = replace(settings, backend=arguments.backend)
    limits = settings.limits
    if arguments.timeout:
        limits = replace(
            limits, wall_clock_s=arguments.timeout, inner_timeout_s=max(arguments.timeout - 5, 1)
        )
    if arguments.memory:
        limits = replace(limits, memory_mb=arguments.memory)
    return replace(settings, limits=limits)


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def run_pipeline(settings: Settings, arguments: argparse.Namespace) -> int:
    editor = load_editor(settings)
    try:
        requests = editor.select(arguments.only)
    except Exception as exc:  # editor errors are user errors, not stack traces
        console.print(Text(f"error: {exc}", style="bold red"))
        return 2

    non_interactive = arguments.auto or arguments.eval
    reviewer = AutoReviewer() if non_interactive else CliReviewer(console)

    try:
        pipeline = Pipeline(settings, reviewer)
    except SandboxUnavailable as exc:
        console.print(Text(f"error: {exc}", style="bold red"))
        return 3

    _print_header(pipeline, len(requests))

    try:
        summary = pipeline.run(requests, reset=not arguments.keep)
    except SandboxUnavailable as exc:
        console.print(Text(f"error: {exc}", style="bold red"))
        return 3
    finally:
        pipeline.close()

    if summary.aborted and not summary.records:
        console.print()
        console.print(Text(f"review did not start: {summary.abort_reason}", style="bold yellow"))
        return 2

    _print_summary(summary)

    if arguments.eval:
        results = evaluate(summary)
        _print_evaluation(results)
        arguments.eval_out.write_text(to_markdown(results), encoding="utf-8")
        console.print()
        console.print(Text(f"results table written to {arguments.eval_out}", style="dim"))
        return 0 if not results.missed and not results.false_positives else 1

    return 0


def _print_header(pipeline: Pipeline, count: int) -> None:
    console.print()
    console.rule("[bold]AiS — sandboxed, execution-verified edit review", style="blue")
    console.print(f"  sandbox   {pipeline.backend.describe()}")
    console.print(f"  project   {pipeline.settings.paths.live_project}")
    console.print(f"  requests  {count}")
    if pipeline.isolation_warning:
        console.print()
        console.print(Text(f"  WARNING: {pipeline.isolation_warning}", style="bold yellow"))


def _print_summary(summary: RunSummary) -> None:
    table = Table(title="run summary", title_justify="left", header_style="dim", expand=True)
    table.add_column("scenario", style="cyan", no_wrap=True)
    table.add_column("verdict", width=8)
    table.add_column("rules fired")
    table.add_column("outcome", width=10)
    table.add_column("commit", width=12, style="dim")

    for record in summary.records:
        verdict = record.report.verdict.value if record.report else "—"
        style = {"PASS": "green", "FLAG": "yellow", "BLOCK": "red"}.get(verdict, "dim")
        outcome_style = {
            Outcome.APPROVED: "green",
            Outcome.REJECTED: "red",
            Outcome.ERROR: "bold magenta",
        }[record.outcome]
        table.add_row(
            record.request.request_id,
            Text(verdict, style=style),
            ", ".join(record.report.rule_ids) if record.report else (record.error or "—"),
            Text(record.outcome.value, style=outcome_style),
            (record.commit_sha or "")[:12],
        )

    console.print()
    console.print(table)
    console.print(
        f"  {len(summary.approved)} approved · {len(summary.rejected)} rejected · "
        f"{len(summary.errored)} errored     audit run id: {summary.run_id}"
    )
    if summary.aborted:
        console.print(
            Text(f"  run stopped early: {summary.abort_reason}", style="yellow")
        )


def _print_evaluation(results: Evaluation) -> None:
    table = Table(title="evaluation", title_justify="left", header_style="dim", expand=True)
    table.add_column("scenario", style="cyan", no_wrap=True)
    table.add_column("truth", width=8)
    table.add_column("verdict", width=8)
    table.add_column("tests", width=14)
    table.add_column("sandbox", width=8, justify="right")
    table.add_column("result")

    for result in results.results:
        style = {
            "detected": "green",
            "detected (other rule)": "yellow",
            "clean pass": "green",
            "MISSED": "bold red",
            "FALSE POSITIVE": "bold red",
            "ERROR": "bold magenta",
        }.get(result.classification, "")
        table.add_row(
            result.request_id,
            result.expected,
            result.verdict,
            result.tests,
            f"{result.sandbox_seconds:.2f}s",
            Text(result.classification, style=style),
        )

    console.print()
    console.print(table)
    console.print()
    console.print(
        f"  detection rate        [bold]{results.detection_rate:.0%}[/]  "
        f"({len(results.detected)}/{len(results.planted)} planted edits flagged)"
    )
    console.print(
        f"  caught by right rule  [bold]{results.correct_reason_rate:.0%}[/]  "
        f"({len(results.right_reason)}/{len(results.planted)})"
    )
    console.print(
        f"  false positive rate   [bold]{results.false_positive_rate:.0%}[/]  "
        f"({len(results.false_positives)}/{len(results.benign)} benign edits flagged)"
    )
    console.print(
        f"  sandbox latency       median {results.median_latency:.2f}s · "
        f"mean {results.mean_latency:.2f}s · total {results.total_latency:.1f}s"
    )


# --------------------------------------------------------------------------
# inspection subcommands
# --------------------------------------------------------------------------


def show_rules() -> int:
    table = Table(title="verifier rule set", title_justify="left", header_style="dim", expand=True)
    table.add_column("rule id", style="cyan", no_wrap=True)
    table.add_column("what it looks for")
    for rule in rule_catalogue():
        table.add_row(rule["id"], f"[bold]{rule['title']}[/]\n{rule['description']}")
    console.print()
    console.print(table)
    return 0


def show_audit(settings: Settings) -> int:
    path = settings.paths.audit_db
    if not path.is_file():
        console.print(Text(f"no audit log yet at {path}. Run the demo first.", style="yellow"))
        return 1

    with AuditLog(path) as log:
        runs = log.runs()
        if not runs:
            console.print(Text("the audit log is empty", style="yellow"))
            return 1

        latest = runs[0]
        console.print()
        console.print(
            f"[bold]{latest['run_id']}[/]  backend={latest['backend']}  "
            f"isolated={bool(latest['isolated'])}  started={latest['started_at']}"
        )

        table = Table(header_style="dim", expand=True)
        table.add_column("#", width=5, justify="right", style="dim")
        table.add_column("stage", width=24, style="cyan")
        table.add_column("actor", width=16)
        table.add_column("request", width=28, style="dim")
        table.add_column("hash", width=12, style="dim")
        for row in log.events(run_id=latest["run_id"]):
            table.add_row(
                str(row["id"]),
                row["stage"],
                row["actor"],
                row["request_id"] or "",
                row["hash"][:10],
            )
        console.print(table)

        status = log.verify()
        console.print()
        console.print(
            Text(status.describe(), style="bold green" if status.ok else "bold red")
        )
        console.print(
            Text(f"  {log.count()} total events across {len(runs)} run(s) in {path}", style="dim")
        )
    return 0


def show_git_log(settings: Settings) -> int:
    root = settings.paths.live_project
    if not (root / ".git").exists():
        console.print(Text(f"no mediated project at {root}. Run the demo first.", style="yellow"))
        return 1
    repo = gitops.ensure_repo(root)
    console.print()
    console.print(f"[bold]git log — {root}[/]")
    for line in gitops.log_lines(repo, limit=40, all_branches=True):
        console.print(f"  {line}")
    console.print()
    console.print(Text(f"  branches: {', '.join(gitops.branch_names(repo))}", style="dim"))
    console.print(
        Text(
            "  every commit here was applied only after a reviewer approved it; each\n"
            "  request is reviewed on its own branch off the same baseline",
            style="dim",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
