"""Unit tests for hashing and the password policy. No app, no database."""

import pytest
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.security import dummy_hash, hash_password, validate_password, verify_password
from app.core.settings import Settings
from tests.conftest import FAST_HASHER

# --- hashing, with the production hasher ------------------------------------

pytestmark = pytest.mark.usefixtures("real_password_hashing")


def test_hashing_uses_argon2id_and_a_fresh_salt() -> None:
    first = hash_password("correct horse battery")
    second = hash_password("correct horse battery")

    assert first.startswith("$argon2id$")
    assert first != second


def test_production_hashing_uses_the_recommended_parameters() -> None:
    """Guards both the app's choice of hasher and the fixture that restores it."""
    parameters = hash_password("anything").split("$")[1:4]

    assert parameters == PasswordHash.recommended().hash("anything").split("$")[1:4]
    assert parameters != FAST_HASHER.hash("anything").split("$")[1:4]


def test_verify_accepts_the_right_password_only() -> None:
    hashed = hash_password("correct horse battery")

    assert verify_password("correct horse battery", hashed) == (True, None)
    assert verify_password("wrong", hashed) == (False, None)


def test_verify_offers_an_upgrade_for_an_outdated_hash() -> None:
    weak = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8192),)).hash("pass")

    valid, upgraded = verify_password("pass", weak)

    assert valid is True
    assert upgraded is not None
    assert verify_password("pass", upgraded) == (True, None)


def test_the_dummy_hash_costs_as_much_as_a_real_one() -> None:
    """Same algorithm and parameters, or it wouldn't hide anything."""
    real_parameters = hash_password("anything").split("$")[1:4]

    assert dummy_hash().split("$")[1:4] == real_parameters
    assert dummy_hash() is dummy_hash()  # computed once


# --- the policy -------------------------------------------------------------


@pytest.fixture(scope="module")
def strict() -> Settings:
    return Settings(
        _env_file=None,
        debug=False,
        secret_key="test-secret-key-that-is-long-enough-for-production",
        allowed_hosts=["blog.example.com"],
    )


@pytest.fixture(scope="module")
def relaxed() -> Settings:
    return Settings(_env_file=None, debug=True)


@pytest.mark.parametrize(
    ("password", "problems"),
    [
        ("a long and unusual passphrase", []),
        ("x" * 12, []),
        ("x" * 128, []),
        ("x" * 11, ["Password must be at least 12 characters."]),
        ("x" * 129, ["Password must be at most 128 characters."]),
        ("123456789012", ["Password can't be entirely numeric."]),
        ("jane.doe@example.com", ["Password is too similar to your email or username."]),
        ("janedoe_2026", ["Password is too similar to your email or username."]),
        (
            "1234567",
            ["Password must be at least 12 characters.", "Password can't be entirely numeric."],
        ),
    ],
)
def test_the_strict_policy(strict: Settings, password: str, problems: list[str]) -> None:
    found = validate_password(password, strict, user_inputs=["jane.doe@example.com", "janedoe"])

    assert found == problems


@pytest.mark.parametrize(
    ("password", "problems"),
    [
        ("12345678", []),  # debug checks only the length
        ("janedoe1", []),
        ("x" * 7, ["Password must be at least 8 characters."]),
        ("x" * 129, ["Password must be at most 128 characters."]),
    ],
)
def test_the_debug_policy(relaxed: Settings, password: str, problems: list[str]) -> None:
    found = validate_password(password, relaxed, user_inputs=["jane.doe@example.com", "janedoe"])

    assert found == problems
