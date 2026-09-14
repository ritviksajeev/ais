"""The Mediator: the trust boundary between an editor agent and the filesystem.

Every real path in AiS lives behind this class. An editor agent hands over an
:class:`~ais.models.EditRequest` carrying project-relative paths and proposed
content -- never a handle, never an absolute path -- and the Mediator decides:

* whether the request stays inside the scope it is allowed to touch
  (:meth:`plan`, which refuses anything that escapes the project root);
* what the sandbox is allowed to see (:meth:`materialize`, which clones the
  computed closure and nothing else);
* what the human will be shown (:meth:`compute_diff`);
* and, only after a human approves, what gets written (:meth:`apply`).

The real files are untouched until :meth:`apply` runs. A rejected request never
reaches it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ais import patchkit
from ais.config import Settings
from ais.mediator import closure as closure_mod
from ais.mediator import gitops
from ais.models import ClonedFile, EditRequest, SandboxPlan

#: Paths inside the project that a request may never target, whatever it claims.
PROTECTED_PREFIXES = (".git/", ".ais_run/")


class MediationError(Exception):
    """Raised when the Mediator cannot carry out a request at all."""


class ScopeViolation(MediationError):
    """Raised when a request reaches for something outside its permitted scope.

    This is the first line of defence and it fires before any sandbox is
    created: a request naming ``../../.ssh/id_rsa`` is refused on paperwork,
    not on execution.
    """


class Mediator:
    """Mediates all filesystem access for the pipeline."""

    #: Branch prefix for the per-request branches the Mediator creates.
    BRANCH_PREFIX = "ais/"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.paths.live_project
        self._repo = None
        self._baseline: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def seed(self, force: bool = False) -> str:
        """Create the live project from the pristine template and commit a baseline.

        The template under ``sample_project/`` is never edited. Each demo run
        works on a fresh copy of it, so a run is repeatable and an approved
        commit from a previous run cannot leak into the next one.
        """
        template = self.settings.paths.template_project
        if not template.is_dir():
            raise MediationError(f"template project not found at {template}")

        if force and self.root.exists():
            shutil.rmtree(self.root)

        if not self.root.exists():
            shutil.copytree(
                template,
                self.root,
                ignore=shutil.ignore_patterns(*closure_mod.IGNORED_DIRS),
            )

        self._repo = gitops.ensure_repo(self.root)
        if gitops.head_sha(self._repo) is None:
            self._baseline = gitops.commit_all(self._repo, "Baseline: project under mediation")
        elif not gitops.is_clean(self._repo):
            self._baseline = gitops.commit_all(self._repo, "Baseline: sync working tree")
        else:
            self._baseline = gitops.head_sha(self._repo)
        return self._baseline

    @property
    def repo(self):
        if self._repo is None:
            self._repo = gitops.ensure_repo(self.root)
        return self._repo

    def baseline_sha(self) -> str | None:
        return self._baseline or gitops.head_sha(self.repo)

    def begin_request(self, request_id: str) -> str:
        """Put the project back on the baseline, on a branch of this request's own.

        Every proposed edit is reviewed against identical starting content, and
        an approved edit becomes a single commit on its own branch -- the same
        shape as a pull request, and the reason the evaluation numbers do not
        depend on the order the requests arrive in.
        """
        branch = f"{self.BRANCH_PREFIX}{request_id}"
        gitops.checkout_fresh_branch(self.repo, branch, self.baseline_sha())
        return branch

    # -- scope -------------------------------------------------------------

    def _resolve(self, logical: str) -> Path:
        """Turn a project-relative path into a real one, or refuse to.

        Refuses absolute paths, parent-directory traversal, protected internals
        and symlinks that point out of the project -- checked against the
        *resolved* path, so a symlink planted inside the project cannot be used
        as a tunnel out of it.
        """
        if not logical or not logical.strip():
            raise ScopeViolation("empty target path")
        if logical.startswith("/") or (len(logical) > 1 and logical[1] == ":"):
            raise ScopeViolation(f"absolute path is not addressable by an editor: {logical!r}")
        if "\x00" in logical:
            raise ScopeViolation(f"null byte in target path: {logical!r}")

        normalised = Path(logical).as_posix()
        if any(part == ".." for part in Path(normalised).parts):
            raise ScopeViolation(f"parent-directory traversal in target: {logical!r}")
        if any(normalised.startswith(prefix) for prefix in PROTECTED_PREFIXES):
            raise ScopeViolation(f"target is inside protected internals: {logical!r}")

        root_real = self.root.resolve()
        candidate = (root_real / normalised).resolve()
        if candidate != root_real and root_real not in candidate.parents:
            raise ScopeViolation(f"target escapes the project root: {logical!r}")
        return candidate

    def plan(self, request: EditRequest) -> SandboxPlan:
        """Validate a request and decide what its sandbox may contain."""
        if not request.targets:
            raise ScopeViolation(f"{request.request_id}: request names no targets")

        declared = set(request.targets)
        supplied = set(request.proposed)
        if declared != supplied:
            missing = sorted(declared - supplied)
            extra = sorted(supplied - declared)
            raise ScopeViolation(
                f"{request.request_id}: declared targets and supplied content disagree "
                f"(missing content for {missing}, undeclared content for {extra})"
            )

        for target in request.targets:
            self._resolve(target)  # raises on anything out of scope

        result = closure_mod.compute(self.root, request.targets, dict(request.proposed))

        files = []
        for path, reason in result.reasons.items():
            real = self._resolve(path)
            if real.is_file():
                content = patchkit.read_text_exact(str(real))
            elif path in request.proposed:
                content = ""  # a file this request creates: nothing to clone yet
                reason = "new file created by this edit"
            else:
                raise MediationError(f"{request.request_id}: closure names a missing file {path!r}")
            files.append(
                ClonedFile(
                    path=path,
                    reason=reason,
                    sha256=patchkit.sha256_text(content),
                    bytes=len(content.encode("utf-8")),
                )
            )

        return SandboxPlan(
            request_id=request.request_id, files=tuple(files), project_root=self.root
        )

    # -- sandbox input -----------------------------------------------------

    def materialize(self, plan: SandboxPlan) -> Path:
        """Clone the planned closure into a fresh directory for a sandbox to ship.

        Only the closure is copied. Nothing else from the project, and nothing
        at all from outside it, is reachable from what the sandbox receives.
        """
        workspace = self.settings.paths.sandboxes / plan.request_id / "workspace"
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)

        for cloned in plan.files:
            source = self.root / cloned.path
            destination = workspace / cloned.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                shutil.copy2(source, destination)
            else:
                destination.touch()  # placeholder for a file the patch creates
        return workspace

    def compute_diff(self, request: EditRequest) -> str:
        """The unified diff between what is on disk now and what is proposed.

        This single artefact is what the sandbox executes, what the human reads,
        and what :meth:`apply` later commits. There is deliberately only one.
        """
        changes: dict[str, tuple[str | None, str | None]] = {}
        for path, proposed in request.proposed.items():
            real = self._resolve(path)
            current = patchkit.read_text_exact(str(real)) if real.is_file() else None
            changes[path] = (current, proposed)
        return patchkit.make_patch(changes)

    # -- outcome -----------------------------------------------------------

    def apply(self, request: EditRequest, diff: str, message: str) -> str:
        """Apply an approved diff to the real files and commit it.

        The diff is applied with the same :mod:`ais.patchkit` code the sandbox
        used, and the result is then checked against the content the sandbox
        actually executed. If they differ by a single byte the change is rolled
        back and the approval is refused: a human approved a specific behaviour,
        and anything else must not reach the file.
        """
        if not diff.strip():
            raise MediationError(f"{request.request_id}: refusing to apply an empty diff")

        before = {
            path: (
                patchkit.read_text_exact(str(self._resolve(path)))
                if self._resolve(path).is_file()
                else None
            )
            for path in request.proposed
        }

        try:
            changed = patchkit.apply_patch(str(self.root), diff)
            self._verify_applied(request)
        except Exception:
            self._restore(before)
            raise

        return gitops.commit_paths(self.repo, changed, message)

    def _verify_applied(self, request: EditRequest) -> None:
        for path, proposed in request.proposed.items():
            actual = patchkit.read_text_exact(str(self._resolve(path)))
            if patchkit.sha256_text(actual) != patchkit.sha256_text(proposed):
                raise MediationError(
                    f"{request.request_id}: {path} does not match the verified content "
                    f"after applying the approved diff -- change rolled back"
                )

    def _restore(self, before: dict[str, str | None]) -> None:
        for path, content in before.items():
            real = self._resolve(path)
            if content is None:
                real.unlink(missing_ok=True)
            else:
                patchkit.write_text_exact(str(real), content)

    def discard(self, plan: SandboxPlan) -> None:
        """Destroy a rejected request's sandbox directory. The real files are untouched."""
        sandbox = self.settings.paths.sandboxes / plan.request_id
        if sandbox.exists():
            shutil.rmtree(sandbox, ignore_errors=True)

    def rollback(self, sha: str) -> str:
        """Revert a previously approved commit. The escape hatch for a bad approval."""
        return gitops.revert(self.repo, sha)

    def read_file(self, logical: str) -> str:
        """Current content of a project file, for showing a reviewer the original."""
        return patchkit.read_text_exact(str(self._resolve(logical)))
