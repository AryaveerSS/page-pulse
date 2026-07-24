"""
Pure parsing logic over already-fetched HTML.

Every function here takes a string and returns a value - no network calls,
no exceptions tied to HTTP. That's deliberate: it means Task B's tests can
exercise the parsing logic directly with hand-written HTML fixtures, with
no need to mock httpx or hit the network.
"""
import re

from bs4 import BeautifulSoup

_WHITESPACE_RE = re.compile(r"\s+")

# Tags whose text doesn't count as "content" for word-count purposes.
_NON_CONTENT_TAGS = ("script", "style", "noscript", "template")


def extract_title(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.string:
        return soup.title.string.strip() or None
    return None


def extract_meta_description(soup: BeautifulSoup) -> str | None:
    tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if tag and tag.get("content"):
        content = tag["content"].strip()
        return content or None
    return None


def count_h1(soup: BeautifulSoup) -> int:
    return len(soup.find_all("h1"))


def count_images_and_missing_alt(soup: BeautifulSoup) -> tuple[int, int]:
    """Returns (total_images, images_missing_alt).

    "Missing alt" means the attribute is absent OR present-but-empty/whitespace,
    since an empty alt is only valid for decorative images and we can't tell
    intent from markup alone - we count it as missing for audit purposes.
    """
    images = soup.find_all("img")
    missing = sum(1 for img in images if not img.get("alt") or not img["alt"].strip())
    return len(images), missing


def approximate_word_count(soup: BeautifulSoup) -> int:
    """
    Counts words in visible body content only.

    Design decision: <head> (title, meta tags) is deliberately excluded -
    it isn't content a visitor reads, so including it would inflate the
    count and make it a worse proxy for "how much is there to read here."
    Work on a copy so callers can keep using the original `soup` afterward.
    """
    text_soup = BeautifulSoup(str(soup), "lxml")
    if text_soup.head:
        text_soup.head.decompose()
    for tag in text_soup(_NON_CONTENT_TAGS):
        tag.decompose()
    text = text_soup.get_text(separator=" ")
    words = _WHITESPACE_RE.sub(" ", text).strip()
    return len(words.split(" ")) if words else 0


def parse_html(html: str) -> dict:
    """Run the full parsing pipeline and return a plain dict of results."""
    soup = BeautifulSoup(html, "lxml")
    image_count, images_missing_alt = count_images_and_missing_alt(soup)
    return {
        "title": extract_title(soup),
        "meta_description": extract_meta_description(soup),
        "h1_count": count_h1(soup),
        "image_count": image_count,
        "images_missing_alt": images_missing_alt,
        "word_count": approximate_word_count(soup),
    }
