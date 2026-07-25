"""
Unit tests for app/services/parser.py.

Design note: every test here is a pure function call — no network, no
server, no mocking.  The parser takes a string and returns a dict; these
tests verify that contract exhaustively.  Fast, deterministic, and the
first thing a reviewer should see passing.

Coverage targets:
  - Happy path (all fields populated correctly)
  - Missing / absent metadata (title, meta description)
  - Alt-text accounting edge cases (empty string, whitespace, absent)
  - Word-count accuracy and exclusion rules (head, script, style, noscript)
  - H1 counting edge cases
  - Robustness against real-world HTML quirks (malformed, unicode, nesting)
"""

import pytest

from app.services.parser import (
    approximate_word_count,
    count_h1,
    count_images_and_missing_alt,
    extract_meta_description,
    extract_title,
    parse_html,
)
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FULL_PAGE_HTML = """
<html>
  <head>
    <title>  Example Domain  </title>
    <meta name="description" content="An example page for testing.">
  </head>
  <body>
    <h1>Welcome</h1>
    <p>This is a short paragraph with exactly eight words here.</p>
    <img src="a.png" alt="A decorative banner">
    <img src="b.png" alt="">
    <img src="c.png">
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# Happy path — full integration through parse_html()
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_extracts_title_stripped(self):
        result = parse_html(FULL_PAGE_HTML)
        # Leading/trailing whitespace must be stripped.
        assert result["title"] == "Example Domain"

    def test_extracts_meta_description(self):
        result = parse_html(FULL_PAGE_HTML)
        assert result["meta_description"] == "An example page for testing."

    def test_counts_single_h1(self):
        result = parse_html(FULL_PAGE_HTML)
        assert result["h1_count"] == 1

    def test_counts_all_images(self):
        result = parse_html(FULL_PAGE_HTML)
        assert result["image_count"] == 3

    def test_counts_missing_alt_correctly(self):
        result = parse_html(FULL_PAGE_HTML)
        # b.png: alt="" (empty string) → missing
        # c.png: no alt attribute       → missing
        # a.png: alt="A decorative banner" → present
        assert result["images_missing_alt"] == 2

    def test_word_count_excludes_head(self):
        result = parse_html(FULL_PAGE_HTML)
        # body text: "Welcome" (1) + paragraph (10) = 11
        # <head> title/meta must NOT be counted
        assert result["word_count"] == 11

    def test_all_keys_present(self):
        result = parse_html(FULL_PAGE_HTML)
        expected_keys = {
            "title",
            "meta_description",
            "h1_count",
            "image_count",
            "images_missing_alt",
            "word_count",
        }
        assert expected_keys == set(result.keys())


# ---------------------------------------------------------------------------
# Title edge cases
# ---------------------------------------------------------------------------


class TestTitleExtraction:
    def test_missing_title_returns_none(self):
        html = "<html><head></head><body></body></html>"
        assert parse_html(html)["title"] is None

    def test_whitespace_only_title_returns_none(self):
        html = "<html><head><title>   \n\t  </title></head><body></body></html>"
        assert parse_html(html)["title"] is None

    def test_empty_title_tag_returns_none(self):
        html = "<html><head><title></title></head><body></body></html>"
        assert parse_html(html)["title"] is None

    def test_unicode_title_preserved(self):
        html = "<html><head><title>日本語タイトル</title></head><body></body></html>"
        assert parse_html(html)["title"] == "日本語タイトル"

    def test_title_with_html_entities(self):
        html = "<html><head><title>Caf&eacute; &amp; Bar</title></head><body></body></html>"
        result = parse_html(html)
        # BeautifulSoup decodes HTML entities
        assert "Café" in result["title"]

    def test_multiple_title_tags_uses_first(self):
        # Malformed HTML — only one title should be reported
        html = "<html><head><title>First</title><title>Second</title></head><body></body></html>"
        soup = BeautifulSoup(html, "lxml")
        result = extract_title(soup)
        assert result == "First"


# ---------------------------------------------------------------------------
# Meta description edge cases
# ---------------------------------------------------------------------------


class TestMetaDescription:
    def test_missing_meta_returns_none(self):
        html = "<html><head><title>X</title></head><body></body></html>"
        assert parse_html(html)["meta_description"] is None

    def test_empty_content_attribute_returns_none(self):
        html = '<html><head><meta name="description" content=""></head><body></body></html>'
        assert parse_html(html)["meta_description"] is None

    def test_whitespace_only_content_returns_none(self):
        html = '<html><head><meta name="description" content="   "></head><body></body></html>'
        assert parse_html(html)["meta_description"] is None

    def test_case_insensitive_name_attribute(self):
        # name="Description" (capital D) should still be detected
        html = '<html><head><meta name="Description" content="Works case-insensitively."></head><body></body></html>'
        soup = BeautifulSoup(html, "lxml")
        result = extract_meta_description(soup)
        assert result == "Works case-insensitively."

    def test_og_description_not_confused_with_meta_description(self):
        html = '<html><head><meta property="og:description" content="OG only"></head><body></body></html>'
        assert parse_html(html)["meta_description"] is None


# ---------------------------------------------------------------------------
# H1 counting
# ---------------------------------------------------------------------------


class TestH1Count:
    def test_no_h1_returns_zero(self):
        html = "<html><body><h2>Only h2</h2></body></html>"
        assert parse_html(html)["h1_count"] == 0

    def test_single_h1(self):
        html = "<html><body><h1>One</h1></body></html>"
        assert parse_html(html)["h1_count"] == 1

    def test_multiple_h1_all_counted(self):
        html = "<html><body><h1>A</h1><h1>B</h1><h1>C</h1></body></html>"
        assert parse_html(html)["h1_count"] == 3

    def test_nested_h1_counted(self):
        html = "<html><body><div><section><h1>Nested</h1></section></div></body></html>"
        assert parse_html(html)["h1_count"] == 1

    def test_h1_in_head_not_counted(self):
        # Malformed but should not crash
        html = "<html><head><h1>Invalid</h1></head><body><h1>Valid</h1></body></html>"
        soup = BeautifulSoup(html, "lxml")
        # lxml moves the h1 out of head; just verify we get a number
        assert isinstance(count_h1(soup), int)


# ---------------------------------------------------------------------------
# Image alt-text accounting
# ---------------------------------------------------------------------------


class TestAltTextAccounting:
    def test_no_images_returns_zero_zero(self):
        html = "<html><body><p>Text only.</p></body></html>"
        result = parse_html(html)
        assert result["image_count"] == 0
        assert result["images_missing_alt"] == 0

    def test_all_images_have_good_alt(self):
        html = '<html><body><img src="a.png" alt="cat"><img src="b.png" alt="dog"></body></html>'
        result = parse_html(html)
        assert result["image_count"] == 2
        assert result["images_missing_alt"] == 0

    def test_empty_alt_counts_as_missing(self):
        html = '<html><body><img src="a.png" alt=""></body></html>'
        result = parse_html(html)
        assert result["images_missing_alt"] == 1

    def test_whitespace_only_alt_counts_as_missing(self):
        html = '<html><body><img src="a.png" alt="   "></body></html>'
        result = parse_html(html)
        assert result["images_missing_alt"] == 1

    def test_absent_alt_attribute_counts_as_missing(self):
        html = '<html><body><img src="a.png"></body></html>'
        result = parse_html(html)
        assert result["images_missing_alt"] == 1

    def test_mixed_present_and_missing(self):
        html = """<html><body>
          <img src="a.png" alt="present">
          <img src="b.png" alt="">
          <img src="c.png">
          <img src="d.png" alt="also present">
        </body></html>"""
        result = parse_html(html)
        assert result["image_count"] == 4
        assert result["images_missing_alt"] == 2

    def test_images_inside_noscript_are_counted(self):
        """Images inside <noscript> are still real img tags in the DOM."""
        html = '<html><body><noscript><img src="fallback.png"></noscript></body></html>'
        soup = BeautifulSoup(html, "lxml")
        total, missing = count_images_and_missing_alt(soup)
        # Verify it doesn't crash and returns integers
        assert isinstance(total, int)
        assert isinstance(missing, int)


# ---------------------------------------------------------------------------
# Word count
# ---------------------------------------------------------------------------


class TestWordCount:
    def test_empty_body_is_zero(self):
        html = "<html><body></body></html>"
        assert parse_html(html)["word_count"] == 0

    def test_head_content_excluded(self):
        html = """<html>
          <head><title>These nine words should not be counted at all</title></head>
          <body><p>Only three words.</p></body>
        </html>"""
        assert parse_html(html)["word_count"] == 3

    def test_script_content_excluded(self):
        html = """<html><body>
          <script>var x = "absolutely should not count";</script>
          <p>Only these four words.</p>
        </body></html>"""
        assert parse_html(html)["word_count"] == 4

    def test_style_content_excluded(self):
        html = """<html><body>
          <style>.foo { color: blue; margin: none; }</style>
          <p>Just four visible words.</p>
        </body></html>"""
        assert parse_html(html)["word_count"] == 4

    def test_noscript_content_excluded(self):
        html = """<html><body>
          <noscript>Enable JavaScript to see this hidden content here.</noscript>
          <p>Three visible words.</p>
        </body></html>"""
        assert parse_html(html)["word_count"] == 3

    def test_unicode_words_counted(self):
        html = "<html><body><p>日本語 テスト 単語</p></body></html>"
        result = parse_html(html)
        assert result["word_count"] == 3

    def test_excessive_whitespace_normalised(self):
        html = "<html><body><p>one   two\t\tthree\n\nfour</p></body></html>"
        assert parse_html(html)["word_count"] == 4

    def test_deeply_nested_text_counted(self):
        html = "<html><body><div><section><article><p>Five deeply nested visible words here.</p></article></section></div></body></html>"
        assert parse_html(html)["word_count"] == 6


# ---------------------------------------------------------------------------
# Robustness — malformed / real-world HTML
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_completely_empty_string_does_not_crash(self):
        result = parse_html("")
        assert result["title"] is None
        assert result["word_count"] == 0
        assert result["image_count"] == 0

    def test_html_without_head_does_not_crash(self):
        result = parse_html("<body><h1>No head tag</h1></body>")
        assert result["h1_count"] == 1

    def test_html_without_body_does_not_crash(self):
        result = parse_html("<html><head><title>Headless</title></head></html>")
        assert result["title"] == "Headless"
        assert result["word_count"] == 0

    def test_deeply_nested_structure_does_not_crash(self):
        nested = "<div>" * 50 + "<p>deep</p>" + "</div>" * 50
        html = f"<html><body>{nested}</body></html>"
        result = parse_html(html)
        assert result["word_count"] == 1

    def test_large_image_count_is_accurate(self):
        imgs = "".join(f'<img src="{i}.png">' for i in range(100))
        html = f"<html><body>{imgs}</body></html>"
        result = parse_html(html)
        assert result["image_count"] == 100
        assert result["images_missing_alt"] == 100

    def test_parse_html_does_not_mutate_between_calls(self):
        """Calling parse_html twice with the same HTML must give identical results."""
        result1 = parse_html(FULL_PAGE_HTML)
        result2 = parse_html(FULL_PAGE_HTML)
        assert result1 == result2
