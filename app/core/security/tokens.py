"""
The two kinds of token.

Access token: a signed JWT, short-lived, sent in the ``Authorization`` header.
It is verified by its signature alone, with no database lookup for the token
itself, and so it can't be revoked; that is why it lives only minutes.

Refresh token: a long random string, long-lived, kept in an HttpOnly cookie.
It means nothing by itself: the server looks up its hash in the database,
which is what makes it revocable.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from bson import ObjectId

from app.core.settings import Settings

# HMAC with SHA-256: one shared secret both signs and verifies. Fine while a
# single service does both.
ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - a claim value, not a secret


def create_access_token(user_id: ObjectId, settings: Settings) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
        "type": ACCESS_TOKEN_TYPE,
    }
    return jwt.encode(claims, settings.secret_key.get_secret_value(), algorithm=ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> ObjectId | None:
    """The user id the token was issued to, or None if it isn't a valid one."""
    try:
        claims = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            # Never take the algorithm from the token itself: naming the one
            # we accept is what stops "alg: none" and key-confusion attacks.
            algorithms=[ALGORITHM],
            options={"require": ["sub", "iat", "exp", "type"]},
        )
    except jwt.InvalidTokenError:  # bad signature, expired, malformed, ...
        return None
    subject = claims["sub"]
    if claims["type"] != ACCESS_TOKEN_TYPE or not ObjectId.is_valid(subject):
        return None
    return ObjectId(subject)


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """
    What is stored instead of the token, so a leaked database holds no usable
    tokens. A fast hash is enough here, unlike for passwords: the token is 48
    random bytes, which can't be guessed however fast the hash is.
    """
    return hashlib.sha256(token.encode()).hexdigest()
