"""Round-trip, safety and integrity tests for the shared patch engine.

``patchkit`` is the one piece of code that runs on both sides of the trust
boundary -- inside the sandbox where the diff is verified, and in the Mediator
where the approved diff is committed. If it can be made to apply two different
results from one patch, the whole "what you approved is what was applied"
property collapses, so it gets tested harder than anything else here.
"""

from __future__ import annotations

import random

import pytest

from ais.patchkit import (
    PatchError,
    read_text_exact,
    write_text_exact,
    apply_patch,
    apply_to_text,
    iter_paths,
    make_file_patch,
    make_patch,
    parse_patch,
    summarise,
)

SOURCE = (
    "import os\n"
    "\n"
    "\n"
    "def total(items):\n"
    "    return sum(items)\n"
    "\n"
    "\n"
    "def label(name):\n"
    '    return name.strip().lower()\n'
)


def round_trip(tmp_path, path, old, new):
    """Generate a patch from old->new, apply it to old, assert we land on new."""
    patch = make_patch({path: (old, new)})
    assert patch, "expected a non-empty patch for a real change"
    target = tmp_path / path
    if old is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_exact(str(target), old)
    apply_patch(str(tmp_path), patch)
    result = read_text_exact(str(target)) if target.exists() else None
    assert result == new
    return patch


class TestRoundTrip:
    @pytest.mark.parametrize(
        "label,old,new",
        [
            ("single line", SOURCE, SOURCE.replace("sum(items)", "sum(items) or 0")),
            ("append", SOURCE, SOURCE + "\n\ndef extra():\n    return 1\n"),
            ("prepend", SOURCE, "# header\n" + SOURCE),
            ("delete a block", SOURCE, SOURCE.replace("def label(name):\n    return name.strip().lower()\n", "")),
            ("whole rewrite", SOURCE, "x = 1\n"),
            ("empty original", "", "hello\n"),
            ("blank-line context", "a\n\n\nb\n", "a\n\n\nB\n"),
            ("crlf preserved", "a\r\nb\r\n", "a\r\nB\r\n"),
            ("unicode preserved", "café ☕\nnaïve\n", "café ☕\nNAÏVE\n"),
            ("line separator char stays inline", "a b\nc\n", "a b\nC\n"),
            ("tabs and trailing spaces", "a\t\n  \nb\n", "a\t\n  \nB\n"),
        ],
    )
    def test_generated_patch_reproduces_the_target(self, tmp_path, label, old, new):
        round_trip(tmp_path, "mod.py", old, new)

    @pytest.mark.parametrize(
        "label,old,new",
        [
            ("neither side terminated", "one\ntwo\nthree", "one\nTWO\nthree"),
            ("gains a final newline", "one\ntwo", "one\ntwo\n"),
            ("loses a final newline", "one\ntwo\n", "one\ntwo"),
            ("single unterminated line", "solo", "SOLO"),
            ("unterminated cr", "a\r\nb\r", "a\r\nB\r"),
        ],
    )
    def test_final_newline_is_preserved_exactly(self, tmp_path, label, old, new):
        patch = round_trip(tmp_path, "mod.py", old, new)
        assert "\\ No newline at end of file" in patch

    def test_nested_path(self, tmp_path):
        round_trip(tmp_path, "pkg/sub/mod.py", SOURCE, SOURCE + "# tail\n")

    def test_file_creation(self, tmp_path):
        round_trip(tmp_path, "brand_new.py", None, "def f():\n    return 42\n")

    def test_file_deletion(self, tmp_path):
        round_trip(tmp_path, "mod.py", SOURCE, None)

    def test_many_scattered_edits(self, tmp_path):
        lines = SOURCE.split("\n")
        random.seed(11)
        for index in sorted(random.sample(range(len(lines) - 1), 3), reverse=True):
            lines[index] = "# touched\n" + lines[index]
        round_trip(tmp_path, "mod.py", SOURCE, "\n".join(lines))

    def test_identical_content_yields_no_patch(self):
        assert make_patch({"mod.py": (SOURCE, SOURCE)}) == ""
        assert make_file_patch("mod.py", SOURCE, SOURCE) == ""

    def test_multi_file_patch(self, tmp_path):
        # write_text_exact, not Path.write_text: the latter translates \n to
        # \r\n on Windows, and patchkit then reads the file byte-exactly and
        # sees context that does not match the patch it generated.
        (tmp_path / "tests").mkdir()
        write_text_exact(str(tmp_path / "mod.py"), SOURCE)
        write_text_exact(str(tmp_path / "tests" / "test_mod.py"), "assert True\n")
        patch = make_patch(
            {
                "mod.py": (SOURCE, SOURCE + "# a\n"),
                "tests/test_mod.py": ("assert True\n", "assert True\n# b\n"),
            }
        )
        assert sorted(apply_patch(str(tmp_path), patch)) == ["mod.py", "tests/test_mod.py"]
        assert read_text_exact(str(tmp_path / "mod.py")).endswith("# a\n")
        assert read_text_exact(str(tmp_path / "tests" / "test_mod.py")).endswith("# b\n")

    def test_multi_file_patch_is_ordered_deterministically(self):
        changes = {
            "z.py": ("a\n", "b\n"),
            "a.py": ("a\n", "b\n"),
            "m.py": ("a\n", "b\n"),
        }
        assert list(iter_paths(make_patch(changes))) == ["a.py", "m.py", "z.py"]


class TestPathConfinement:
    @pytest.mark.parametrize(
        "escape", ["../evil.py", "/etc/passwd", "sub/../../evil.py", "./../../x.py"]
    )
    def test_patch_cannot_write_outside_the_root(self, tmp_path, escape):
        patch = (
            f"diff --git a/{escape} b/{escape}\n"
            f"--- /dev/null\n+++ b/{escape}\n@@ -0,0 +1 @@\n+pwned\n"
        )
        with pytest.raises(PatchError):
            apply_patch(str(tmp_path), patch)


class TestAtomicity:
    def test_a_failing_second_file_leaves_the_first_untouched(self, tmp_path):
        write_text_exact(str(tmp_path / "ok.py"), "a\n")
        patch = (
            "diff --git a/ok.py b/ok.py\n--- a/ok.py\n+++ b/ok.py\n@@ -1 +1 @@\n-a\n+A\n"
            "diff --git a/missing.py b/missing.py\n--- a/missing.py\n+++ b/missing.py\n"
            "@@ -1 +1 @@\n-zzz\n+Z\n"
        )
        with pytest.raises(PatchError):
            apply_patch(str(tmp_path), patch)
        assert read_text_exact(str(tmp_path / "ok.py")) == "a\n", "partial write escaped the rollback"


class TestStrictness:
    def test_context_mismatch_is_an_error_not_a_guess(self, tmp_path):
        write_text_exact(str(tmp_path / "a.py"), "one\ntwo\nthree\n")
        patch = (
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            "@@ -1,3 +1,3 @@\n one\n-DIFFERENT\n+two2\n three\n"
        )
        with pytest.raises(PatchError, match="context mismatch"):
            apply_patch(str(tmp_path), patch)

    def test_modifying_an_absent_file_is_an_error(self, tmp_path):
        patch = "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ b/gone.py\n@@ -1 +1 @@\n-a\n+b\n"
        with pytest.raises(PatchError, match="absent"):
            apply_patch(str(tmp_path), patch)

    def test_creating_an_existing_file_is_an_error(self, tmp_path):
        write_text_exact(str(tmp_path / "a.py"), "already here\n")
        patch = "diff --git a/a.py b/a.py\n--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+new\n"
        with pytest.raises(PatchError, match="already exists"):
            apply_patch(str(tmp_path), patch)

    def test_hunk_past_end_of_file_is_an_error(self, tmp_path):
        write_text_exact(str(tmp_path / "a.py"), "one\n")
        patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -50,1 +50,1 @@\n-x\n+y\n"
        with pytest.raises(PatchError, match="past end of file"):
            apply_patch(str(tmp_path), patch)

    def test_a_hunk_with_no_file_header_is_rejected(self):
        with pytest.raises(PatchError, match="precedes any file header"):
            parse_patch("@@ -1 +1 @@\n-a\n+b\n")

    def test_a_patch_with_no_file_headers_at_all_is_rejected(self):
        with pytest.raises(PatchError, match="no file headers"):
            parse_patch("just some prose, not a patch\n")

    def test_dangling_minus_header_is_rejected(self):
        with pytest.raises(PatchError, match="no '\\+\\+\\+' partner"):
            parse_patch("--- a/x.py\nnot a plus line\n")

    def test_malformed_hunk_header_is_rejected(self):
        with pytest.raises(PatchError, match="malformed hunk header"):
            parse_patch("--- a/x.py\n+++ b/x.py\n@@ nonsense @@\n")

    def test_diffing_a_file_absent_on_both_sides_is_an_error(self):
        with pytest.raises(PatchError, match="neither side"):
            make_file_patch("ghost.py", None, None)


class TestInspection:
    def test_summarise_counts_lines_and_files(self):
        patch = make_patch(
            {"a.py": ("one\ntwo\n", "one\nTWO\n"), "b.py": (None, "x\ny\n")}
        )
        assert summarise(patch) == {"files": 2, "added": 3, "removed": 1}

    def test_summarise_of_an_empty_patch(self):
        assert summarise("") == {"files": 0, "added": 0, "removed": 0}

    def test_added_and_removed_lines_are_exposed_for_rules(self):
        patch = parse_patch(make_patch({"a.py": ("keep\ndrop\n", "keep\nadd\n")}))[0]
        # Hunk bodies are stored without their line terminators, which is the
        # form the rule engine wants when it greps a diff for dangerous calls.
        assert patch.removed_lines == ["drop"]
        assert patch.added_lines == ["add"]

    def test_apply_to_text_is_usable_without_a_filesystem(self):
        patch = parse_patch(make_patch({"a.py": ("one\n", "two\n")}))[0]
        assert apply_to_text("one\n", patch) == "two\n"
