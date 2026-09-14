"""The interactive review step: diff and execution report side by side.

This is the human decision layer. It prints what the sandbox saw next to what
the change does, and waits. Nothing is written to a real file until someone
answers.
"""

from __future__ import annotations

import sys
import time

from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from ais.models import Decision
from ais.review import render
from ais.review.base import Reviewer, ReviewPresentation


class ReviewAborted(Exception):
    """Raised when the reviewer quits the run. Remaining requests are untouched."""


CHOICES = {
    "a": "approve — apply the diff to the real file as a git commit",
    "r": "reject — discard the sandbox; the real file is never touched",
    "d": "show the diff again",
    "o": "show the current content of the target files",
    "t": "show the full execution trace",
    "w": "show why each file was cloned into the sandbox",
    "q": "quit the run (everything still pending is left alone)",
}


class CliReviewer(Reviewer):
    """Prompts a human at the terminal."""

    name = "reviewer:cli"

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._index = 0
        self._total = 0

    def opening(self, total: int) -> None:
        self._total = total
        self._index = 0
        if not sys.stdin.isatty():
            raise ReviewAborted(
                "interactive review needs a terminal, but stdin is not a TTY. "
                "Re-run with --auto to use the non-interactive reviewer."
            )
        self.console.print()
        self.console.print(
            Text(
                f"{total} edit request(s) to review. Nothing is written to a real file "
                f"until you approve it.",
                style="bold",
            )
        )

    def review(self, presentation: ReviewPresentation) -> Decision:
        self._index += 1
        console = self.console
        started = time.monotonic()

        console.print()
        console.print(render.separator(presentation.request.title, self._index, self._total))
        console.print(render.request_panel(presentation))
        console.print(render.diff_panel(presentation.diff))
        console.print(
            render.execution_panel(presentation.report, presentation.isolation_warning)
        )

        while True:
            console.print()
            choice = Prompt.ask(
                Text.from_markup(
                    "[bold]approve[/] / [bold]reject[/] / "
                    "[dim]d[/]iff / [dim]o[/]riginal / [dim]t[/]race / "
                    "[dim]w[/]hy-cloned / [dim]q[/]uit"
                ),
                choices=list(CHOICES),
                default="r",
                show_choices=False,
            )

            if choice == "a":
                return self._decide(presentation, True, started)
            if choice == "r":
                return self._decide(presentation, False, started)
            if choice == "q":
                raise ReviewAborted("review stopped by the reviewer")
            if choice == "d":
                console.print(render.diff_panel(presentation.diff))
            elif choice == "o":
                console.print(render.original_panel(presentation))
            elif choice == "t":
                self._print_full_trace(presentation)
            elif choice == "w":
                console.print(render.request_panel(presentation))

    def _decide(self, presentation: ReviewPresentation, approved: bool, started: float) -> Decision:
        verb = "approved" if approved else "rejected"
        reason = Prompt.ask(
            Text(f"why {verb}? (optional)", style="dim"), default="", show_default=False
        )
        self.console.print(
            Text(
                f"{verb.upper()}",
                style="bold green" if approved else "bold red",
            )
        )
        return Decision(
            request_id=presentation.request_id,
            approved=approved,
            reviewer=self.name,
            reason=reason.strip() or f"{verb} at the CLI without a stated reason",
            review_seconds=round(time.monotonic() - started, 2),
        )

    def _print_full_trace(self, presentation: ReviewPresentation) -> None:
        trace = presentation.report.sandbox.trace
        if not trace:
            self.console.print(Text("no events were traced during this run", style="dim"))
            return
        for event in trace:
            marker = "!" if event.escapes_workspace else " "
            self.console.print(Text(f"{marker} {event.summary()}", style="dim"))

    def closing(self, decisions: list[Decision]) -> None:
        approved = sum(1 for d in decisions if d.approved)
        self.console.print()
        self.console.print(
            Text(
                f"{approved} approved, {len(decisions) - approved} rejected "
                f"out of {len(decisions)} reviewed.",
                style="bold",
            )
        )
