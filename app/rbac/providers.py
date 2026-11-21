"""
The role repository's provider, on its own.

It would normally sit in ``dependencies``, but that module imports
``auth.dependencies`` (for the current user), and auth needs the role
repository at sign-up. Keeping this one provider free of auth lets auth
depend on it without a circular import.
"""

from typing import Annotated

from fastapi import Depends

from app.core.database import DbDep
from app.rbac.repository import RoleRepository


async def get_role_repository(db: DbDep) -> RoleRepository:
    return RoleRepository(db)


RoleRepositoryDep = Annotated[RoleRepository, Depends(get_role_repository)]
