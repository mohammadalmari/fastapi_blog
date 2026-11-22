"""Unit tests for how emails, usernames and passwords are accepted. No app."""

import pytest
from pydantic import ValidationError

from app.auth.schemas import RegisterRequest
from app.users.schemas import ProfileUpdate, normalize_login_identifier

VALID = {"email": "jane@example.com", "username": "jane", "password": "correct horse battery"}


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("jane", "jane"),
        ("Jane_Doe", "jane_doe"),
        ("  jane  ", "jane"),
        ("abc", "abc"),
        ("a" * 30, "a" * 30),
    ],
)
def test_usernames_are_normalized(typed: str, stored: str) -> None:
    assert RegisterRequest.model_validate({**VALID, "username": typed}).username == stored


@pytest.mark.parametrize(
    "username", ["", "ab", "a" * 31, "has space", "jane@example.com", "jane-doe", "josé", 123]
)
def test_invalid_usernames_are_refused(username: object) -> None:
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate({**VALID, "username": username})


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("jane@example.com", "jane@example.com"),
        ("Jane@Example.COM", "jane@example.com"),
        # "e" + a combining accent becomes the single composed character.
        ("jose\u0301@example.com", "josé@example.com"),
    ],
)
def test_emails_are_normalized(typed: str, stored: str) -> None:
    assert RegisterRequest.model_validate({**VALID, "email": typed}).email == stored


@pytest.mark.parametrize("email", ["", "not-an-email", "jane@", "@example.com", "a b@example.com"])
def test_invalid_emails_are_refused(email: str) -> None:
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate({**VALID, "email": email})


@pytest.mark.parametrize("password", ["", "x" * 129])
def test_passwords_outside_the_length_bounds_are_refused(password: str) -> None:
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate({**VALID, "password": password})


@pytest.mark.parametrize(
    ("typed", "lookup"),
    [
        ("jane@example.com", ("email", "jane@example.com")),
        (" JANE@Example.com ", ("email", "jane@example.com")),
        ("jose\u0301@example.com", ("email", "josé@example.com")),
        ("not an email@", ("email", "not an email@")),  # can't match anyone
        ("jane", ("username", "jane")),
        (" Jane ", ("username", "jane")),
    ],
)
def test_the_login_identifier_is_normalized_like_sign_up(
    typed: str, lookup: tuple[str, str]
) -> None:
    assert normalize_login_identifier(typed) == lookup


def test_nobody_can_be_called_me() -> None:
    """`/users/me` is the caller's own account; a user called "me" would have no profile page."""
    with pytest.raises(ValidationError):
        ProfileUpdate.model_validate({"username": "me"})


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ({"display_name": "  Jane Doe  "}, {"display_name": "Jane Doe"}),
        ({"display_name": "x" * 50}, {"display_name": "x" * 50}),
        (
            {"display_name": "\u0645\u062d\u0645\u062f"},
            {"display_name": "\u0645\u062d\u0645\u062f"},
        ),
        ({"bio": "Line one.\nLine two."}, {"bio": "Line one.\nLine two."}),
        ({"bio": "x" * 500}, {"bio": "x" * 500}),
        ({"bio": "", "display_name": ""}, {"bio": "", "display_name": ""}),
        ({"username": "Jane_2"}, {"username": "jane_2"}),
        ({}, {}),
    ],
)
def test_profile_fields_people_actually_send_are_accepted(
    sent: dict[str, str], kept: dict[str, str]
) -> None:
    assert ProfileUpdate.model_validate(sent).model_dump(exclude_none=True) == kept


@pytest.mark.parametrize(
    "sent",
    [
        {"display_name": "x" * 51},
        {"display_name": "Jane\nDoe"},  # a name is one line
        {"display_name": "Jane\u202eDoe"},  # right-to-left override: text shown reversed
        {"display_name": "Jane\u200b"},  # zero-width space
        {"bio": "x" * 501},
        {"bio": "Hello\u0000"},
        {"bio": "Hello\u202e"},
        {"username": "jane doe"},
        {"display_name": 5},
    ],
)
def test_profile_fields_that_would_mislead_or_overflow_are_refused(sent: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ProfileUpdate.model_validate(sent)
