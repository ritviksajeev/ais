"""The sandbox closure: what a request is allowed to see, and why."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ais.mediator import closure


class TestModuleMap:
    def test_maps_flat_modules(self, project):
        modules = closure.project_modules(project)
        assert modules["pricing"] == "pricing.py"
        assert modules["textkit"] == "textkit.py"

    def test_maps_nested_modules_both_ways(self, project):
        modules = closure.project_modules(project)
        assert modules["tests.test_pricing"] == "tests/test_pricing.py"
        assert modules["test_pricing"] == "tests/test_pricing.py"

    def test_skips_ignored_directories(self, project):
        (project / "__pycache__").mkdir(exist_ok=True)
        (project / "__pycache__" / "junk.py").write_text("x = 1\n", encoding="utf-8")
        assert "junk" not in closure.project_modules(project)


class TestIgnoredDirectoryHandling:
    """Regression: the ignore list must apply below the root, not above it."""

    def test_a_root_inside_an_ignored_directory_still_resolves(self, tmp_path):
        # The live project lives under `.ais_run/`, which is itself an ignored
        # name. Matching ignored names against the whole path emptied every
        # closure and silently shipped sandboxes with no tests in them.
        nested = tmp_path / ".ais_run" / "project"
        nested.parent.mkdir()
        shutil.copytree(Path(__file__).resolve().parent.parent / "sample_project", nested)

        assert closure.test_files(nested), "test discovery broke inside an ignored ancestor"
        result = closure.compute(nested, ["textkit.py"])
        assert "tests/test_textkit.py" in result.paths


class TestImportScanning:
    def test_finds_plain_imports(self):
        assert "os" in closure.imported_names("import os\n")

    def test_finds_from_imports_and_their_root(self):
        names = closure.imported_names("from pkg.sub import thing\n")
        assert {"pkg", "pkg.sub"} <= names

    def test_ignores_relative_imports(self):
        assert closure.imported_names("from . import sibling\n") == set()

    def test_unparseable_source_yields_nothing_rather_than_raising(self):
        # A syntax error belongs in the sandbox's test output, where a human can
        # see it -- not as a Mediator crash before any sandbox exists.
        assert closure.imported_names("def broken(:\n") == set()


class TestClosure:
    def test_target_and_its_covering_test(self, project):
        result = closure.compute(project, ["textkit.py"])
        assert set(result.paths) == {"textkit.py", "tests/test_textkit.py", "conftest.py"}

    def test_is_minimal_and_excludes_unrelated_modules(self, project):
        result = closure.compute(project, ["textkit.py"])
        assert "pricing.py" not in result.paths
        assert "inventory.py" not in result.paths

    def test_pulls_in_imported_dependencies(self, project):
        result = closure.compute(project, ["inventory.py"])
        assert result.reasons["pricing.py"] == "imported by an edit target"

    def test_pulls_in_modules_the_covering_tests_need(self, project):
        # Editing pricing.py drags in test_inventory.py (it reaches pricing via
        # inventory), which in turn means inventory.py must be there too or the
        # sandbox would report an ImportError instead of real behaviour.
        result = closure.compute(project, ["pricing.py"])
        assert "tests/test_inventory.py" in result.paths
        assert "inventory.py" in result.paths

    def test_always_includes_the_harness(self, project):
        result = closure.compute(project, ["textkit.py"])
        assert result.reasons["conftest.py"] == "test harness file"

    def test_follows_imports_of_a_file_that_does_not_exist_yet(self, project):
        result = closure.compute(project, ["brand_new.py"], {"brand_new.py": "import textkit\n"})
        assert "textkit.py" in result.paths

    def test_is_deterministic(self, project):
        first = closure.compute(project, ["pricing.py"]).paths
        second = closure.compute(project, ["pricing.py"]).paths
        assert first == second == tuple(sorted(first))

    @pytest.mark.parametrize("target", ["pricing.py", "inventory.py", "textkit.py"])
    def test_every_closure_can_actually_run_its_tests(self, project, target):
        result = closure.compute(project, [target])
        assert any(path.startswith("tests/") for path in result.paths)
        assert "conftest.py" in result.paths
