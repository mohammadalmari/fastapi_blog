"""Password hashing (Argon2id) and the password policy."""

import re
from collections.abc import Iterable
from difflib import SequenceMatcher

from pwdlib import PasswordHash

from app.core.settings import Settings
from app.core.settings.security import PASSWORD_MAX_LENGTH

# pwdlib's recommended settings: currently Argon2id with default parameters.
# A hash stores its own parameters, so if a later version raises them, old
# hashes still verify and `verify_password` returns an upgraded hash to store.
# That does not cover a change of algorithm: if `recommended()` ever stops
# listing Argon2, existing hashes raise UnknownHashError. Check this when
# upgrading pwdlib, and list the hashers explicitly if it happens.
_password_hash = PasswordHash.recommended()

# A password this similar to the user's email/username is rejected.
_MAX_SIMILARITY = 0.7


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """
    Check a password against its stored hash.

    Returns ``(is_valid, new_hash)``. ``new_hash`` is set when the stored hash
    uses outdated parameters; save it to upgrade the user transparently.
    """
    return _password_hash.verify_and_update(password, password_hash)


def validate_password(
    password: str,
    settings: Settings,
    *,
    user_inputs: Iterable[str] = (),
) -> list[str]:
    """
    Check a new password against the policy.

    Returns the list of problems; an empty list means the password is accepted.
    ``user_inputs`` are values such as the email and username that the password
    must not resemble.

    In DEBUG only the length is checked, to keep local iteration painless.
    """
    problems: list[str] = []

    if len(password) < settings.password_min_length:
        problems.append(f"Password must be at least {settings.password_min_length} characters.")
    if len(password) > PASSWORD_MAX_LENGTH:
        problems.append(f"Password must be at most {PASSWORD_MAX_LENGTH} characters.")

    if not settings.password_strict:
        return problems

    if password.isdigit():
        problems.append("Password can't be entirely numeric.")
    if _resembles(password, user_inputs):
        problems.append("Password is too similar to your email or username.")

    return problems


def _resembles(password: str, user_inputs: Iterable[str]) -> bool:
    password = password.lower()
    for user_input in user_inputs:
        # Compare against the whole value and its parts: "jane.doe@example.com"
        # also yields "jane", "doe", "example", "com".
        for part in {user_input.lower(), *re.split(r"\W+", user_input.lower())}:
            if len(part) < 3:
                continue
            if SequenceMatcher(a=password, b=part).quick_ratio() >= _MAX_SIMILARITY:
                return True
    return False
