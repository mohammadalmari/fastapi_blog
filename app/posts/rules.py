"""Pure text rules for posts: slugs, tags, titles and excerpts. No database, no FastAPI."""

import re
import unicodedata

SLUG_MAX_LENGTH = 80
TAG_MAX_LENGTH = 30
EXCERPT_LENGTH = 200
# How much of the content an excerpt is made from. Two reasons for a bound.
# Only the start can end up in the excerpt anyway. And the patterns below take
# time that grows with the square of the input on text built for it (a long
# run of "[" with no "]"): 100,000 such characters took 26 seconds, on a route
# any author can call. 2,000 take a few milliseconds at worst.
EXCERPT_SOURCE_LENGTH = 2_000

# Zero-width non-joiner and joiner. Invisible, but part of how Persian and
# several Indian scripts spell words, so titles may contain them.
_JOINERS = "\u200c\u200d"


def _kind(character: str) -> str:
    """The first letter of the Unicode category: L letter, M mark, N number, ..."""
    return unicodedata.category(character)[0]


def slugify(title: str) -> str:
    """
    The readable part of a post's URL: "Hello, World!" becomes "hello-world".

    Letters and digits of every script are kept, together with the marks that
    belong to them: a slug is for people to read, and many scripts write their
    vowels as marks on a consonant. Spaces, dashes and underscores separate
    words; everything else is dropped. A title with nothing readable in it
    gives "post".
    """
    # NFKC folds look-alike forms (full-width letters, ligatures) into the
    # ordinary ones, so two titles that look the same slug the same.
    words: list[str] = []
    word = ""
    for character in unicodedata.normalize("NFKC", title).lower():
        if _kind(character) in "LMN":
            word += character
        elif character.isspace() or character in "-_":
            words.append(word)
            word = ""
        # Anything else (punctuation, symbols, invisible characters) is dropped.
    words.append(word)
    slug = "-".join(part for part in words if part)
    return slug[:SLUG_MAX_LENGTH].rstrip("-") or "post"


def normalize_tag(tag: str) -> str:
    """ " Mongo DB " and "mongo-db" are one tag."""
    return "-".join(unicodedata.normalize("NFKC", tag).casefold().split())


def tag_problem(tag: str) -> str | None:
    """Why a (normalized) tag is refused, or None if it is fine."""
    if not 1 <= len(tag) <= TAG_MAX_LENGTH:
        return f"A tag has 1 to {TAG_MAX_LENGTH} characters"
    for character in tag:
        # Letters, digits and their marks, in any script, plus the signs real
        # tags use: c++, c#, node.js, .net, web_dev. Not allowed: slashes and
        # other punctuation, and above all invisible characters, which would
        # let "py<zero-width joiner>thon" pose as "python".
        if _kind(character) not in "LMN" and character not in "+#.-_":
            return "A tag may contain letters, digits and + # . - _"
    if not any(_kind(character) in "LN" for character in tag):
        return "A tag needs at least one letter or digit"
    if _kind(tag[0]) == "M":
        return "A tag can't start with an accent"
    return None


def has_hidden_characters(text: str) -> bool:
    """
    Control and formatting characters: NUL, line breaks, and the invisible
    marks that reverse or hide text. In a title or excerpt they can make it
    display as something other than what it is.
    """
    return any(_kind(character) == "C" and character not in _JOINERS for character in text)


# An opening fence up to its closing fence, or to the end if it is never
# closed (which also covers a block cut off by the bound above).
_CODE_BLOCK = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MARKUP = re.compile(r"[#>*_`~]")


def make_excerpt(content: str, limit: int = EXCERPT_LENGTH) -> str:
    """
    A short summary of Markdown content, for lists and previews.

    Not a Markdown parser: it drops code blocks, keeps the text of links and
    images, removes the common markup characters, and cuts at a word. It does
    not remove HTML, so the result is as untrusted as the content it came from.
    """
    text = _CODE_BLOCK.sub(" ", content[:EXCERPT_SOURCE_LENGTH])
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _MARKUP.sub("", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
