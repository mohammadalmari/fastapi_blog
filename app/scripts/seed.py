"""
Create the optional built-in roles and the first administrator.

    uv run python -m app.scripts.seed --email you@example.com --username you

The password is asked for, not passed as an argument, so that it doesn't end
up in the shell history. To make an account that already exists an admin:

    uv run python -m app.scripts.seed --email you@example.com --promote

Safe to run again: it never overwrites a role or a password.
"""

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from app.auth.schemas import RegisterRequest
from app.core.database import Database, create_client, ensure_indexes
from app.core.security import hash_password, validate_password
from app.core.settings import Settings, get_settings
from app.rbac.permissions import ADMIN_ROLE, OPTIONAL_ROLES
from app.rbac.repository import DuplicateRoleError, RoleRepository
from app.rbac.sync import sync_rbac
from app.users.models import User
from app.users.repository import DuplicateUserError, UserRepository
from app.users.schemas import normalize_email


class SeedError(Exception):
    """Seeding was refused; the message says why. Nothing was changed."""


@dataclass
class SeedResult:
    # created: a new admin account. promoted: an existing account was given
    # the admin role. unchanged: it already had it.
    outcome: Literal["created", "promoted", "unchanged"]
    roles_created: list[str] = field(default_factory=list)


async def seed(
    db: Database,
    settings: Settings,
    *,
    email: str,
    username: str | None = None,
    password: str | None = None,
    promote: bool = False,
) -> SeedResult:
    users = UserRepository(db)

    # Decide everything before writing anything, so a refusal changes nothing.
    if promote:
        account = await _account_to_promote(users, email)
        new_admin = None
    else:
        account = None
        new_admin = await _validated_new_admin(users, settings, email, username, password)

    await sync_rbac(db)
    roles = RoleRepository(db)
    result = SeedResult(outcome="unchanged")

    for name, definition in OPTIONAL_ROLES.items():
        try:
            await roles.create(
                name=name, description=definition.description, permissions=definition.permissions
            )
        except DuplicateRoleError:
            continue  # it exists, perhaps edited: leave it alone
        result.roles_created.append(name)

    admin_role = await roles.get_by_name(ADMIN_ROLE)
    if admin_role is None:
        raise SeedError("The admin role is missing")  # can't happen: sync_rbac just made it

    if account is not None:
        if admin_role.id not in account.role_ids:
            await users.add_role(account.id, admin_role.id)
            result.outcome = "promoted"
    elif new_admin is not None:
        try:
            await users.create(
                email=new_admin.email,
                username=new_admin.username,
                # The blocking hash, directly: this is a command-line script
                # with nothing else waiting on the event loop.
                password_hash=hash_password(new_admin.password),
                role_ids=[admin_role.id],
            )
        except DuplicateUserError as error:  # taken since we checked
            raise SeedError(f"That {error.field} is already taken") from error
        result.outcome = "created"
    return result


async def _account_to_promote(users: UserRepository, email: str) -> User:
    try:
        normalized = normalize_email(email)
    except ValueError as error:
        raise SeedError(f"email: {error}") from error
    account = await users.get_by_email(normalized)
    if account is None:
        raise SeedError(f"No account with the email {normalized}")
    if not account.is_active:
        # It couldn't log in, and wouldn't count as an active admin.
        raise SeedError(f"The account {account.username} is deactivated")
    return account


async def _validated_new_admin(
    users: UserRepository,
    settings: Settings,
    email: str,
    username: str | None,
    password: str | None,
) -> RegisterRequest:
    # The same checks sign-up applies.
    try:
        data = RegisterRequest.model_validate(
            {"email": email, "username": username, "password": password}
        )
    except ValidationError as error:
        problems = [f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors()]
        raise SeedError("; ".join(problems)) from error
    problems = validate_password(data.password, settings, user_inputs=[data.email, data.username])
    if problems:
        raise SeedError(" ".join(problems))

    existing = await users.get_by_email(data.email)
    if existing is not None:
        # Sign-up is open and emails aren't verified, so this account may not
        # be the operator's: anyone could have registered the address first.
        # Making it an admin has to be asked for by name.
        raise SeedError(
            f"An account with the email {data.email} already exists (username "
            f"{existing.username}). If it is yours, give it the admin role with --promote."
        )
    if await users.get_by_login(data.username) is not None:
        raise SeedError(f"The username {data.username} is already taken")
    return data


def _run(*, email: str, username: str | None, password: str | None, promote: bool) -> SeedResult:
    """Connect using the real settings, seed, disconnect."""

    async def run() -> SeedResult:
        settings = get_settings()
        client = create_client(settings)
        try:
            db = client[settings.database_name]
            await ensure_indexes(db)
            return await seed(
                db, settings, email=email, username=username, password=password, promote=promote
            )
        finally:
            await client.close()

    return asyncio.run(run())


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create the built-in roles and the first admin.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--username", help="for a new account")
    parser.add_argument(
        "--promote",
        action="store_true",
        help="give the admin role to the existing account with this email",
    )
    args = parser.parse_args(argv)

    password: str | None = None
    if not args.promote:
        if not args.username:
            parser.error("--username is required to create an admin (or use --promote)")
        password = getpass.getpass("Password for the admin: ")
        if getpass.getpass("Again: ") != password:
            sys.exit("The two passwords differ.")

    try:
        result = _run(
            email=args.email, username=args.username, password=password, promote=args.promote
        )
    except SeedError as error:
        # A string makes the exit status 1 and prints the message to stderr.
        sys.exit(f"Not seeded: {error}")

    print(f"Roles created: {', '.join(result.roles_created) or 'none (they already existed)'}")
    print(
        {
            "created": f"Admin created: {args.email}",
            "promoted": f"{args.email} now has the admin role. Its password is unchanged.",
            "unchanged": f"{args.email} already had the admin role. Nothing changed.",
        }[result.outcome]
    )


if __name__ == "__main__":
    main()
