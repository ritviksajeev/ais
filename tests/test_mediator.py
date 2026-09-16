"""The Mediator: the only component allowed to touch real files.

These tests are about the trust boundary rather than the plumbing. The
properties that matter: a request cannot name a path outside its project, a
rejected request leaves the real files byte-identical, and an approved diff
produces exactly the content the sandbox verified -- or nothing at all.
"""

from __future__ import annotations

import pytest
from conftest import make_request

from ais import patchkit
from ais.mediator import MediationError, Mediator, ScopeViolation, gitops


@pytest.fixture
def mediator(settings):
    instance = Mediator(settings)
    instance.seed(force=True)
    return instance


def edit_of(mediator, path, transform):
    """Build a request that applies ``transform`` to a real project file."""
    current = mediator.read_file(path)
    return make_request(
        request_id="req-edit", targets=(path,), proposed={path: transform(current)}
    )


class TestSeeding:
    def test_creates_a_git_repo_with_a_baseline_commit(self, mediator):
        assert (mediator.root / ".git").exists()
        assert mediator.baseline_sha() is not None

    def test_leaves_the_template_untouched(self, mediator, settings):
        template = settings.paths.template_project / "pricing.py"
        before = patchkit.read_text_exact(str(template))
        request = edit_of(mediator, "pricing.py", lambda text: text + "# appended\n")
        mediator.apply(request, mediator.compute_diff(request), "test")
        assert patchkit.read_text_exact(str(template)) == before

    def test_reseeding_returns_to_a_clean_baseline(self, mediator):
        request = edit_of(mediator, "pricing.py", lambda text: text + "# appended\n")
        mediator.apply(request, mediator.compute_diff(request), "test")
        assert "# appended" in mediator.read_file("pricing.py")
        mediator.seed(force=True)
        assert "# appended" not in mediator.read_file("pricing.py")


class TestScope:
    @pytest.mark.parametrize(
        "path",
        [
            "../escape.py",
            "../../etc/passwd",
            "/etc/passwd",
            "sub/../../escape.py",
            ".git/config",
            ".git/hooks/pre-commit",
            "",
            "   ",
        ],
    )
    def test_out_of_scope_targets_are_refused(self, mediator, path):
        request = make_request(targets=(path,), proposed={path: "pwned\n"})
        with pytest.raises(ScopeViolation):
            mediator.plan(request)

    def test_refusal_happens_before_any_sandbox_exists(self, mediator, settings):
        path = "../escape.py"
        request = make_request(targets=(path,), proposed={path: "pwned\n"})
        with pytest.raises(ScopeViolation):
            mediator.plan(request)
        assert not list(settings.paths.sandboxes.glob("*"))

    def test_a_request_with_no_targets_is_refused(self, mediator):
        with pytest.raises(ScopeViolation, match="no targets"):
            mediator.plan(make_request(targets=(), proposed={}))

    def test_declared_targets_must_match_supplied_content(self, mediator):
        request = make_request(
            targets=("pricing.py",), proposed={"pricing.py": "x\n", "textkit.py": "y\n"}
        )
        with pytest.raises(ScopeViolation, match="undeclared content"):
            mediator.plan(request)

    def test_a_symlink_cannot_be_used_as_a_tunnel_out(self, mediator, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("classified\n", encoding="utf-8")
        try:
            (mediator.root / "link.py").symlink_to(secret)
        except (OSError, NotImplementedError) as exc:
            # Windows only allows this for an administrator or in developer
            # mode. Skipping is honest; silently passing would not be.
            pytest.skip(f"cannot create a symlink on this platform: {exc}")
        request = make_request(targets=("link.py",), proposed={"link.py": "pwned\n"})
        with pytest.raises(ScopeViolation, match="escapes the project root"):
            mediator.plan(request)
        assert secret.read_text() == "classified\n"


class TestPlanning:
    def test_records_why_each_file_was_cloned(self, mediator):
        plan = mediator.plan(make_request(targets=("textkit.py",), proposed={"textkit.py": "x\n"}))
        reasons = {entry.path: entry.reason for entry in plan.files}
        assert reasons["textkit.py"] == "edit target"
        assert reasons["tests/test_textkit.py"] == "test that exercises an edit target"

    def test_records_a_content_hash_per_cloned_file(self, mediator):
        plan = mediator.plan(make_request(targets=("textkit.py",), proposed={"textkit.py": "x\n"}))
        entry = next(f for f in plan.files if f.path == "textkit.py")
        assert entry.sha256 == patchkit.sha256_text(mediator.read_file("textkit.py"))


class TestMaterialising:
    def test_clones_exactly_the_closure_and_nothing_else(self, mediator):
        plan = mediator.plan(make_request(targets=("textkit.py",), proposed={"textkit.py": "x\n"}))
        workspace = mediator.materialize(plan)
        present = {p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file()}
        assert present == set(plan.paths)

    def test_the_sandbox_gets_no_git_directory(self, mediator):
        plan = mediator.plan(make_request(targets=("textkit.py",), proposed={"textkit.py": "x\n"}))
        workspace = mediator.materialize(plan)
        assert not (workspace / ".git").exists()


class TestApplying:
    def test_an_approved_diff_produces_exactly_the_verified_content(self, mediator):
        request = edit_of(mediator, "textkit.py", lambda text: text + "\n# approved\n")
        diff = mediator.compute_diff(request)
        mediator.apply(request, diff, "approved")
        assert mediator.read_file("textkit.py") == request.proposed["textkit.py"]

    def test_applying_creates_a_commit(self, mediator):
        before = mediator.baseline_sha()
        request = edit_of(mediator, "textkit.py", lambda text: text + "\n# approved\n")
        sha = mediator.apply(request, mediator.compute_diff(request), "approved")
        assert sha and sha != before

    def test_an_empty_diff_is_refused(self, mediator):
        request = edit_of(mediator, "textkit.py", lambda text: text)
        with pytest.raises(MediationError, match="empty diff"):
            mediator.apply(request, "", "approved")

    def test_a_diff_that_does_not_match_the_approved_content_is_rolled_back(self, mediator):
        # The integrity check: a human approved specific content, so anything
        # else reaching the file must be undone rather than committed.
        request = edit_of(mediator, "textkit.py", lambda text: text + "\n# approved\n")
        honest_diff = mediator.compute_diff(request)
        before = mediator.read_file("textkit.py")

        tampered = dict(request.proposed)
        tampered["textkit.py"] = tampered["textkit.py"] + "# smuggled\n"
        swapped = make_request(
            request_id=request.request_id, targets=request.targets, proposed=tampered
        )
        with pytest.raises(MediationError, match="does not match the verified content"):
            mediator.apply(swapped, honest_diff, "approved")
        assert mediator.read_file("textkit.py") == before

    def test_a_failed_apply_leaves_the_file_untouched(self, mediator):
        before = mediator.read_file("textkit.py")
        stale = (
            "diff --git a/textkit.py b/textkit.py\n--- a/textkit.py\n+++ b/textkit.py\n"
            "@@ -1,2 +1,2 @@\n-this line is not in the file\n+replacement\n"
        )
        request = make_request(targets=("textkit.py",), proposed={"textkit.py": "whatever\n"})
        with pytest.raises(patchkit.PatchError):
            mediator.apply(request, stale, "approved")
        assert mediator.read_file("textkit.py") == before


class TestRejecting:
    def test_discarding_destroys_the_sandbox_and_spares_the_real_file(self, mediator, settings):
        request = edit_of(mediator, "textkit.py", lambda text: text + "\n# never approved\n")
        before = mediator.read_file("textkit.py")
        plan = mediator.plan(request)
        mediator.materialize(plan)
        assert (settings.paths.sandboxes / request.request_id).exists()

        mediator.discard(plan)
        assert not (settings.paths.sandboxes / request.request_id).exists()
        assert mediator.read_file("textkit.py") == before


class TestRollback:
    def test_an_approved_commit_can_be_reverted(self, mediator):
        original = mediator.read_file("textkit.py")
        request = edit_of(mediator, "textkit.py", lambda text: text + "\n# regrettable\n")
        sha = mediator.apply(request, mediator.compute_diff(request), "approved")
        assert "# regrettable" in mediator.read_file("textkit.py")

        mediator.rollback(sha)
        assert mediator.read_file("textkit.py") == original


class TestBranchIsolation:
    def test_each_request_starts_from_the_same_baseline(self, mediator):
        baseline = mediator.read_file("textkit.py")

        mediator.begin_request("first")
        first = edit_of(mediator, "textkit.py", lambda text: text + "\n# first\n")
        mediator.apply(first, mediator.compute_diff(first), "approved")
        assert "# first" in mediator.read_file("textkit.py")

        mediator.begin_request("second")
        assert mediator.read_file("textkit.py") == baseline

    def test_an_approved_commit_survives_on_its_own_branch(self, mediator):
        mediator.begin_request("first")
        first = edit_of(mediator, "textkit.py", lambda text: text + "\n# first\n")
        mediator.apply(first, mediator.compute_diff(first), "approved")
        mediator.begin_request("second")
        assert "ais/first" in [head.name for head in mediator.repo.heads]


def _autocrlf(repo) -> object:
    """Read ``core.autocrlf`` back. GitPython may hand it over as a bool or a str."""
    return repo.config_reader().get_value("core", "autocrlf")


class TestLineEndings:
    """A mediated repository must never translate line endings.

    Git installs on Windows default to ``core.autocrlf=true``, which makes
    ``git checkout`` rewrite LF to CRLF on the way out of the object database.
    The Mediator checks out a fresh branch for every request, so that default
    would hand the sandbox -- and the human reviewer -- different bytes from
    the ones it committed, on one platform only. AiS claims that what a human
    approved is byte-for-byte what reaches the file; a repository that quietly
    rewrites bytes cannot honour that.
    """

    def test_the_mediated_repo_pins_the_setting(self, mediator):
        assert _autocrlf(mediator.repo) in (False, "false")

    def test_a_request_checkout_returns_the_committed_bytes(self, mediator):
        before = patchkit.read_text_exact(str(mediator.root / "pricing.py"))
        mediator.begin_request("req-line-endings")
        after = patchkit.read_text_exact(str(mediator.root / "pricing.py"))
        assert after == before
        assert "\r" not in after

    def test_reopening_corrects_a_repo_that_was_left_translating(self, tmp_path):
        # A repository created by an older AiS, or by a host whose global git
        # config turned translation on after the fact. Opening it has to fix it,
        # which is why the settings are applied on every open rather than once
        # at creation.
        root = tmp_path / "left-translating"
        repo = gitops.ensure_repo(root)
        with repo.config_writer() as config:
            config.set_value("core", "autocrlf", "true")

        source = root / "sample.py"
        patchkit.write_text_exact(str(source), "a = 1\nb = 2\n")
        gitops.commit_all(repo, "baseline")
        baseline = gitops.head_sha(repo)
        # Move on, so that going back to the baseline forces git to write the
        # file out of the object database again -- which is where translation
        # happens. A checkout that has nothing to materialise proves nothing.
        patchkit.write_text_exact(str(source), "a = 1\nb = 2\nc = 3\n")
        gitops.commit_all(repo, "a later commit")
        gitops.close_repo(repo)

        reopened = gitops.ensure_repo(root)
        try:
            gitops.checkout_fresh_branch(reopened, "ais/req-1", baseline)
            assert source.read_bytes() == b"a = 1\nb = 2\n"
            assert _autocrlf(reopened) in (False, "false")
        finally:
            gitops.close_repo(reopened)
