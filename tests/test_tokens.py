"""Unit tests for the token helpers. No app, no database."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from bson import ObjectId

from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_refresh_token,
    new_refresh_token,
)
from app.core.security.tokens import ALGORITHM
from app.core.settings import Settings
from tests.helpers import ACCESS_TOKEN_TTL_MINUTES

USER_ID = ObjectId("665f1c2e8a4b3d0012345678")


def _claims(**changes: object) -> dict[str, object]:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(USER_ID),
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "type": "access",
    }
    return {name: value for name, value in {**claims, **changes}.items() if value is not None}


def test_an_access_token_round_trips(settings: Settings) -> None:
    token = create_access_token(USER_ID, settings)

    assert decode_access_token(token, settings) == USER_ID


def test_access_token_claims(settings: Settings) -> None:
    token = create_access_token(USER_ID, settings)

    claims = jwt.decode(token, settings.secret_key.get_secret_value(), algorithms=[ALGORITHM])
    assert claims["sub"] == str(USER_ID)
    assert claims["type"] == "access"
    assert claims["exp"] - claims["iat"] == ACCESS_TOKEN_TTL_MINUTES * 60


# Signing with our key under HS512 makes PyJWT warn that the key is short for
# that algorithm; the token is meant to be rejected anyway.
@pytest.mark.filterwarnings("ignore:The HMAC key is")
@pytest.mark.parametrize(
    "case",
    [
        "garbage",
        "empty",
        "wrong key",
        "expired",
        "wrong type",
        "no type",
        "no subject",
        "no expiry",
        "subject is not an id",
        "unsigned",
        "another algorithm",
    ],
)
def test_decode_rejects_a_bad_token(settings: Settings, case: str) -> None:
    key = settings.secret_key.get_secret_value()
    past = datetime.now(UTC) - timedelta(seconds=1)
    tokens = {
        "garbage": "not.a.token",
        "empty": "",
        "wrong key": jwt.encode(_claims(), "another-secret-key-of-enough-length", ALGORITHM),
        "expired": jwt.encode(_claims(exp=past), key, ALGORITHM),
        "wrong type": jwt.encode(_claims(type="refresh"), key, ALGORITHM),
        "no type": jwt.encode(_claims(type=None), key, ALGORITHM),
        "no subject": jwt.encode(_claims(sub=None), key, ALGORITHM),
        "no expiry": jwt.encode(_claims(exp=None), key, ALGORITHM),
        "subject is not an id": jwt.encode(_claims(sub="not-an-id"), key, ALGORITHM),
        "unsigned": jwt.encode(_claims(), "", algorithm="none"),
        "another algorithm": jwt.encode(_claims(), key, algorithm="HS512"),
    }

    assert decode_access_token(tokens[case], settings) is None


def test_decode_accepts_the_same_claims_properly_signed(settings: Settings) -> None:
    """The control for the test above."""
    token = jwt.encode(_claims(), settings.secret_key.get_secret_value(), ALGORITHM)

    assert decode_access_token(token, settings) == USER_ID


def test_refresh_tokens_are_long_random_and_stored_hashed() -> None:
    first, second = new_refresh_token(), new_refresh_token()

    assert first != second
    assert len(first) >= 64
    assert hash_refresh_token(first) == hash_refresh_token(first)
    assert hash_refresh_token(first) != hash_refresh_token(second)
    assert first not in hash_refresh_token(first)
