"""
Who may do what. Plain Python with no FastAPI in it, so that services can
import it; the request-handling side is in ``dependencies``.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from bson import ObjectId

from app.core.errors import ForbiddenError
from app.rbac.models import Role
from app.rbac.permissions import Perm
from app.users.models import User

FORBIDDEN = "You don't have permission to do this"


@dataclass(frozen=True)
class Principal:
    """The logged-in user together with what their roles add up to."""

    user: User
    roles: list[Role]
    permissions: frozenset[str]

    def has(self, permission: Perm) -> bool:
        return permission in self.permissions

    def can(self, *, any_: Perm, own: Perm | None = None, owner_id: ObjectId) -> bool:
        """
        The rule for things that have an owner: yes with the ``:any``
        permission, or with the ``:own`` one when the thing is the caller's.

        Some actions have no ``:own`` form (reading drafts: an owner may
        always read theirs, which the caller checks itself), so ``own`` is
        optional. Use this where a "no" isn't an error, as when filtering a
        list; use ``authorize`` where it is.
        """
        if self.has(any_):
            return True
        return own is not None and self.has(own) and owner_id == self.user.id


def authorize(
    principal: Principal, *, any_: Perm, own: Perm | None = None, owner_id: ObjectId
) -> None:
    """``Principal.can``, as a check that raises. Called from a service once
    it has loaded the thing in question."""
    if not principal.can(any_=any_, own=own, owner_id=owner_id):
        raise ForbiddenError(FORBIDDEN)


def ensure_can_grant(principal: Principal, permissions: Iterable[str]) -> None:
    """
    Nobody can give out a permission they don't hold themselves.

    Without this rule, the permission to edit roles would be the permission
    to do anything: its holder could write every permission into a role of
    their own. The same goes for assigning roles. With it, both can be handed
    to someone who is not a full administrator.
    """
    missing = sorted(set(permissions) - principal.permissions)
    if missing:
        raise ForbiddenError(f"You can't grant permissions you don't hold: {', '.join(missing)}")
