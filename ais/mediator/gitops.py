"""Git plumbing for the Mediator.

Approval writes a commit rather than a bare file write, so every change the
pipeline makes to a real file is attributable and revertible with ordinary git
commands. Nothing else in AiS talks to git.
"""

from __future__ import annotations

from pathlib import Path

from git import Actor, Repo
from git.exc import GitError

#: Identity stamped on every commit the Mediator makes. A distinct author makes
#: mediated changes trivially greppable in the history of the project under edit.
MEDIATOR = Actor("AiS Mediator", "mediator@ais.local")


class GitOpsError(Exception):
    """Raised when a git operation the Mediator depends on fails."""


def ensure_repo(path: Path) -> Repo:
    """Open the repo at ``path``, initialising one if it is not a repo yet."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        if (path / ".git").exists():
            return Repo(path)
        repo = Repo.init(path, initial_branch="main")
    except GitError as exc:
        raise GitOpsError(f"could not open or create a repo at {path}: {exc}") from exc

    with repo.config_writer() as config:
        config.set_value("user", "name", MEDIATOR.name)
        config.set_value("user", "email", MEDIATOR.email)
        config.set_value("commit", "gpgsign", "false")
    return repo


def commit_all(repo: Repo, message: str) -> str:
    """Stage everything and commit. Returns the new commit sha."""
    repo.git.add(A=True)
    return _commit(repo, message)


def commit_paths(repo: Repo, paths: list[str], message: str) -> str:
    """Stage exactly ``paths`` and commit. Returns the new commit sha."""
    if not paths:
        raise GitOpsError("refusing to commit with no paths staged")
    try:
        repo.index.add(paths)
    except (GitError, OSError) as exc:
        raise GitOpsError(f"could not stage {paths}: {exc}") from exc
    return _commit(repo, message)


def _commit(repo: Repo, message: str) -> str:
    try:
        commit = repo.index.commit(message, author=MEDIATOR, committer=MEDIATOR)
    except GitError as exc:
        raise GitOpsError(f"commit failed: {exc}") from exc
    return commit.hexsha


def head_sha(repo: Repo) -> str | None:
    """Current HEAD, or ``None`` in a repo with no commits yet."""
    try:
        return repo.head.commit.hexsha
    except (ValueError, GitError):
        return None


def is_clean(repo: Repo) -> bool:
    """True when the working tree has no uncommitted or untracked changes."""
    return not repo.is_dirty(untracked_files=True)


def revert(repo: Repo, sha: str) -> str:
    """Revert ``sha`` with a new commit. This is the rollback path for a bad approval."""
    try:
        repo.git.revert(sha, no_edit=True)
    except GitError as exc:
        raise GitOpsError(f"could not revert {sha[:12]}: {exc}") from exc
    return repo.head.commit.hexsha


def checkout_fresh_branch(repo: Repo, branch: str, start_point: str) -> None:
    """Discard the working tree and put ``branch`` at ``start_point``.

    Each proposed edit is reviewed against the same baseline, on its own branch.
    Without this, scenario N would be diffed against whatever scenario N-1 left
    behind: an edit written against the baseline would silently *revert* an
    earlier approved change, and the evaluation would depend on the order the
    requests happened to arrive in.
    """
    try:
        repo.git.reset("--hard")
        repo.git.clean("-fd")
        repo.git.checkout("-B", branch, start_point)
    except GitError as exc:
        raise GitOpsError(f"could not start branch {branch!r} at {start_point[:12]}: {exc}") from exc


def branch_names(repo: Repo) -> list[str]:
    return sorted(head.name for head in repo.heads)


def log_lines(repo: Repo, limit: int = 20, all_branches: bool = False) -> list[str]:
    """One-line summaries of recent commits, newest first."""
    if head_sha(repo) is None:
        return []
    kwargs = {"max_count": limit}
    if all_branches:
        kwargs["all"] = True
    labels: dict[str, list[str]] = {}
    for head in repo.heads:
        labels.setdefault(head.commit.hexsha, []).append(head.name)
    lines = []
    for commit in repo.iter_commits(**kwargs):
        decoration = f"  ({', '.join(labels[commit.hexsha])})" if commit.hexsha in labels else ""
        lines.append(f"{commit.hexsha[:12]}  {commit.summary}{decoration}")
    return lines
