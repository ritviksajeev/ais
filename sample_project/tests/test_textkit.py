import pytest

from textkit import normalize_whitespace, slugify, title_case, truncate


class TestNormalizeWhitespace:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("  hello   world  ", "hello world"),
            ("a\t\tb", "a b"),
            ("line\nbreak", "line break"),
            ("", ""),
            ("   ", ""),
        ],
    )
    def test_known_values(self, raw, expected):
        assert normalize_whitespace(raw) == expected


class TestSlugify:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Hello World", "hello-world"),
            ("  Mechanical Keyboard (75%)  ", "mechanical-keyboard-75"),
            ("already-a-slug", "already-a-slug"),
            ("!!!", ""),
            ("Ergo   Mouse", "ergo-mouse"),
        ],
    )
    def test_known_values(self, raw, expected):
        assert slugify(raw) == expected

    def test_never_starts_or_ends_with_a_hyphen(self):
        assert not slugify("  ?? edge ??  ").startswith("-")
        assert not slugify("  ?? edge ??  ").endswith("-")


class TestTruncate:
    def test_short_text_is_untouched(self):
        assert truncate("short", 20) == "short"

    def test_exact_length_is_untouched(self):
        assert truncate("12345", 5) == "12345"

    def test_long_text_gets_a_suffix(self):
        assert truncate("abcdefghij", 8) == "abcde..."

    def test_result_never_exceeds_the_limit(self):
        for limit in range(0, 15):
            assert len(truncate("abcdefghij", limit)) <= limit

    def test_custom_suffix(self):
        assert truncate("abcdefghij", 6, suffix="…") == "abcde…"

    def test_negative_limit_rejected(self):
        with pytest.raises(ValueError):
            truncate("abc", -1)


class TestTitleCase:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("the quick brown fox", "The Quick Brown Fox"),
            ("a tale of two cities", "A Tale of Two Cities"),
            ("keyboard  and   mouse", "Keyboard and Mouse"),
            ("THE END", "The End"),
        ],
    )
    def test_known_values(self, raw, expected):
        assert title_case(raw) == expected
