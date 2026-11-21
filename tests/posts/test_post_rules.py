"""Unit tests for slugs, tags, excerpts and what the posts endpoints accept. No app, no database."""

import time

import pytest
from pydantic import ValidationError

from app.posts.rules import SLUG_MAX_LENGTH, make_excerpt, slugify
from app.posts.schemas import FeedParams, PostCreate, PostUpdate

VALID = {"title": "Hello", "content": "World"}

# --- slugs --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Hello World", "hello-world"),
        ("  Hello,   World!  ", "hello-world"),
        ("FastAPI & MongoDB: a love story", "fastapi-mongodb-a-love-story"),
        ("snake_case and kebab-case", "snake-case-and-kebab-case"),
        ("Don't panic", "dont-panic"),
        ("Café déjà vu", "café-déjà-vu"),
        # Letters of any alphabet are kept: a slug is for people to read.
        ("مرحبا بالعالم", "مرحبا-بالعالم"),
        # Including scripts whose vowels are combining marks. Dropping the
        # marks, as an earlier version did, turned this into gibberish.
        ("हिन्दी भाषा", "हिन्दी-भाषा"),
        ("日本語のタイトル", "日本語のタイトル"),
        ("100% done", "100-done"),
        # Nothing readable left: a fixed word.
        ("---", "post"),
        ("!!!", "post"),
        # Invisible and direction-changing characters never reach a URL.
        ("ze\u200bro wi\u200ddth", "zero-width"),
        ("\u202eevil", "evil"),
        ("../../etc/passwd", "etcpasswd"),
    ],
)
def test_slugify(title: str, slug: str) -> None:
    assert slugify(title) == slug


def test_a_long_title_gives_a_slug_of_bounded_length_without_a_trailing_dash() -> None:
    slug = slugify("word " * 100)

    assert len(slug) <= SLUG_MAX_LENGTH
    assert not slug.endswith("-")
    assert slug.startswith("word-word")


# --- excerpts -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "excerpt"),
    [
        ("Plain text.", "Plain text."),
        ("# Title\n\nSome **bold** and _italic_ `code`.", "Title Some bold and italic code."),
        ("See [the docs](https://example.com) now.", "See the docs now."),
        ("![a cat](cat.png) Look!", "a cat Look!"),
        ("Before\n\n```python\nsecret = 1\n```\n\nAfter", "Before After"),
        # A code block that is never closed is still code, not prose.
        ("Intro\n\n```python\nsecret = 1\n", "Intro"),
        ("> quoted\n\n- item one\n- item two", "quoted - item one - item two"),
        ("", ""),
    ],
)
def test_an_excerpt_is_the_content_without_its_markup(content: str, excerpt: str) -> None:
    assert make_excerpt(content) == excerpt


def test_a_long_excerpt_is_cut_at_a_word_and_marked() -> None:
    excerpt = make_excerpt("alpha beta gamma " * 50, limit=30)

    assert excerpt == "alpha beta gamma alpha beta…"
    assert len(excerpt) <= 31


def test_a_code_block_longer_than_what_is_read_does_not_leak_into_the_excerpt() -> None:
    content = "Intro.\n\n```python\n" + "password = 'hunter2'\n" * 500 + "```\n\nThe end."

    assert make_excerpt(content) == "Intro."


@pytest.mark.parametrize("hostile", ["[" * 100_000, "![" * 50_000])
def test_an_excerpt_of_hostile_content_is_still_quick(hostile: str) -> None:
    """
    The attack: content built to make the pattern matching crawl. A hundred
    thousand unclosed brackets took 26 seconds, on a route any author can
    call. Only the start of the content can end up in an excerpt, so only the
    start is looked at. (Without that bound these two inputs take seconds;
    the limit below leaves a wide margin either way.)
    """
    started = time.perf_counter()

    make_excerpt(hostile)

    assert time.perf_counter() - started < 0.5


# --- what a post may contain --------------------------------------------------


def test_a_post_is_tidied_on_the_way_in() -> None:
    post = PostCreate.model_validate(
        {"title": "  Hello  ", "content": "World", "tags": [" FastAPI ", "fastapi", "Mongo DB"]}
    )

    assert post.title == "Hello"
    assert post.excerpt is None
    assert post.tags == ["fastapi", "mongo-db"]  # lowercased, spaces dashed, no duplicates


@pytest.mark.parametrize(
    "tag", ["python", "c++", "c#", "node.js", ".net", "web_dev", "مقالات", "हिन्दी", "2026"]
)
def test_tags_people_actually_use_are_accepted(tag: str) -> None:
    assert PostCreate.model_validate({**VALID, "tags": [tag]}).tags == [tag]


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "x" * 31,
        "no/slash",
        "a,b",
        "<b>",
        # Nothing to read.
        "---",
        "_",
        "...",
        "\u0301",  # a combining accent on its own
        "\u0301abc",  # or in front, where it has no letter to sit on
        # The attack: a tag that looks like "python" but is a different string.
        "py\u200dthon",
        "\u202enohtyp",
    ],
)
def test_unreadable_and_look_alike_tags_are_refused(tag: str) -> None:
    with pytest.raises(ValidationError):
        PostCreate.model_validate({**VALID, "tags": [tag]})


@pytest.mark.parametrize(
    "fields",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        # Invisible characters that can make a title display as something else.
        {"title": "\u202eevil"},
        {"title": "a\u0000b"},
        {"title": "line\nbreak"},
        {"content": ""},
        {"content": "x" * 100_001},
        {"excerpt": "x" * 301},
        {"excerpt": "\u202eevil"},
        {"tags": ["ok"] * 11},
        {"tags": "fastapi"},
        # Not the client's to set.
        {"author_id": "5f0000000000000000000000"},
        {"status": "published"},
        {"slug": "my-own-slug"},
        {"like_count": 1000},
        {"published_at": "2020-01-01T00:00:00Z"},
    ],
)
def test_invalid_posts_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PostCreate.model_validate({**VALID, **fields})


def test_a_title_may_use_the_joiners_some_scripts_need() -> None:
    """Persian spells some words with a zero-width non-joiner inside them."""
    zero_width_non_joiner = "\u200c"
    title = "می" + zero_width_non_joiner + "خواهم"

    assert PostCreate.model_validate({**VALID, "title": title}).title == title


@pytest.mark.parametrize(
    "fields",
    [
        {"title": ""},
        {"content": ""},
        {"tags": ["no/slash"]},
        {"status": "published"},
        {"slug": "x"},
    ],
)
def test_invalid_post_changes_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PostUpdate.model_validate(fields)


def test_a_post_change_may_send_any_subset_of_fields() -> None:
    assert PostUpdate.model_validate({}).model_dump(exclude_none=True) == {}
    assert PostUpdate.model_validate({"tags": []}).tags == []
    assert PostUpdate.model_validate({"excerpt": ""}).excerpt == ""


# --- the feed's filters -------------------------------------------------------


def test_the_feed_filters_are_normalized_and_blank_means_no_filter() -> None:
    assert FeedParams.model_validate({"tag": " Mongo DB ", "author": " Ali "}).tag == "mongo-db"
    assert FeedParams.model_validate({"author": " Ali "}).author == "Ali"
    # What an HTML form sends for an empty box.
    blank = FeedParams.model_validate({"tag": "", "author": "  "})
    assert (blank.tag, blank.author) == (None, None)


@pytest.mark.parametrize("params", [{"tag": "x" * 31}, {"author": "x" * 31}, {"bogus": "1"}])
def test_invalid_feed_filters_are_refused(params: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        FeedParams.model_validate(params)
