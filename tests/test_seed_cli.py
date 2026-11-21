"""
The command line around `seed`: what it asks, what it passes on, how it exits.

The seeding itself is replaced by a recorder, so these need no database; what
seeding does is tested in test_seed.py.
"""

import pytest

from app.scripts import seed as seed_module
from app.scripts.seed import SeedError, SeedResult, main
from tests.helpers import PASSWORD


@pytest.fixture
def passwords(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the operator will type at the two password prompts."""
    typed = [PASSWORD, PASSWORD]
    monkeypatch.setattr(seed_module.getpass, "getpass", lambda prompt: typed.pop(0))
    return typed


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Records how `main` calls the seeding, in place of a real database."""
    recorded: list[dict[str, object]] = []

    def fake_run(**arguments: object) -> SeedResult:
        recorded.append(arguments)
        if arguments["email"] == "taken@example.com":
            raise SeedError("An account with this email already exists")
        return SeedResult(outcome="created", roles_created=["author"])

    monkeypatch.setattr(seed_module, "_run", fake_run)
    return recorded


@pytest.mark.usefixtures("passwords")
def test_main_asks_for_the_password_and_reports(
    calls: list[dict[str, object]], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["--email", "root@example.com", "--username", "root"])

    assert calls == [
        {"email": "root@example.com", "username": "root", "password": PASSWORD, "promote": False}
    ]
    output = capsys.readouterr().out
    assert "author" in output
    assert "Admin created" in output
    assert PASSWORD not in output


def test_main_stops_when_the_two_passwords_differ(
    calls: list[dict[str, object]], passwords: list[str]
) -> None:
    passwords[1] = "something else"

    with pytest.raises(SystemExit) as stopped:
        main(["--email", "root@example.com", "--username", "root"])

    assert stopped.value.code not in (0, None)
    assert calls == []


@pytest.mark.usefixtures("passwords")
def test_main_exits_with_an_error_when_seeding_is_refused(calls: list[dict[str, object]]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--email", "taken@example.com", "--username", "root"])

    assert "already exists" in str(stopped.value.code)


def test_main_promote_asks_for_no_password(
    calls: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_prompt(prompt: str) -> str:
        raise AssertionError("promoting must not ask for a password")

    monkeypatch.setattr(seed_module.getpass, "getpass", no_prompt)

    main(["--email", "jane@example.com", "--promote"])

    assert calls == [
        {"email": "jane@example.com", "username": None, "password": None, "promote": True}
    ]


def test_main_needs_a_username_to_create_an_admin(calls: list[dict[str, object]]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--email", "root@example.com"])

    assert stopped.value.code not in (0, None)
    assert calls == []
