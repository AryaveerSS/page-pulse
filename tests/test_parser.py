"""
Tests for app/services/parser.py.

These are pure unit tests over hand-written HTML fixtures - no network,
no mocking, no server. They're the fast, deterministic core of the test
suite, and they're what makes it safe to change parsing logic later.
"""
from app.services.parser import parse_html

HAPPY_PATH_HTML = """
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


def test_happy_path_extracts_all_fields():
    result = parse_html(HAPPY_PATH_HTML)

    assert result["title"] == "Example Domain"
    assert result["meta_description"] == "An example page for testing."
    assert result["h1_count"] == 1
    assert result["image_count"] == 3
    # b.png has alt="" (empty) and c.png has no alt attribute at all - both count as missing.
    assert result["images_missing_alt"] == 2
    # Body-visible text only: "Welcome" (h1, 1 word) + the paragraph (10 words).
    # <head> content (title, meta) is deliberately excluded - see parser.py.
    assert result["word_count"] == 11


def test_missing_title_and_meta_description_return_none():
    html = "<html><head></head><body><h1>Hi</h1></body></html>"
    result = parse_html(html)

    assert result["title"] is None
    assert result["meta_description"] is None
    assert result["h1_count"] == 1


def test_no_images_returns_zero_counts_not_an_error():
    html = "<html><body><p>Just text, no images at all.</p></body></html>"
    result = parse_html(html)

    assert result["image_count"] == 0
    assert result["images_missing_alt"] == 0


def test_multiple_h1_tags_are_all_counted():
    html = "<html><body><h1>One</h1><h1>Two</h1><h1>Three</h1></body></html>"
    result = parse_html(html)

    assert result["h1_count"] == 3


def test_script_and_style_text_excluded_from_word_count():
    html = """
    <html><body>
      <script>var totallyNotAWord = "should not be counted here";</script>
      <style>.foo { color: red; }</style>
      <p>Only these four words.</p>
    </body></html>
    """
    result = parse_html(html)

    assert result["word_count"] == 4


def test_empty_body_gives_zero_word_count():
    html = "<html><body></body></html>"
    result = parse_html(html)

    assert result["word_count"] == 0


def test_word_count_excludes_head_content():
    """Title/meta text should not inflate the visible word count."""
    html = """
    <html>
      <head><title>Nine extra words that nobody actually reads on the page</title></head>
      <body><p>Only three words.</p></body>
    </html>
    """
    result = parse_html(html)

    assert result["word_count"] == 3


def test_whitespace_only_title_treated_as_missing():
    html = "<html><head><title>   </title></head><body></body></html>"
    result = parse_html(html)

    assert result["title"] is None
