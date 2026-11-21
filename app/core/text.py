"""Checks on text that people will read: what may be hidden in it, and whether anything shows."""

import unicodedata

# Zero-width non-joiner and joiner. Invisible, but part of how Persian and
# several Indian scripts spell words, and of how compound emoji are built.
JOINERS = "\u200c\u200d"
LINE_BREAKS_AND_TABS = "\n\r\t"

_LINE_AND_PARAGRAPH_SEPARATORS = ("Zl", "Zp")


def kind(character: str) -> str:
    """The first letter of the Unicode category: L letter, M mark, N number, ..."""
    return unicodedata.category(character)[0]


def has_hidden_characters(text: str, *, allowed: str = "") -> bool:
    """
    Control and formatting characters: NUL, escape, and the invisible marks
    that reverse or hide text. In something that is displayed they can make
    it read as something other than what it is.

    The joiners are always allowed. ``allowed`` adds more, such as line
    breaks for text that may span several lines.
    """
    for character in text:
        if character in JOINERS or character in allowed:
            continue
        category = unicodedata.category(character)
        if category[0] == "C" or category in _LINE_AND_PARAGRAPH_SEPARATORS:
            return True
    return False


def has_visible_characters(text: str) -> bool:
    """Whether anything would show: a letter, digit, punctuation mark or symbol."""
    return any(kind(character) in "LNPS" for character in text)
