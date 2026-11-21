"""Unit tests for how emails, usernames and passwords are accepted. No app."""

import pytest
from pydantic import ValidationError

from app.auth.schemas import RegisterRequest
from app.users.schemas import normalize_login_identifier

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
