from typing import Annotated

from fastapi import Depends

from app.core.database import DbDep
from app.users.repository import UserRepository


# A provider function rather than `Depends(UserRepository)`: the repository
# stays a plain class that knows nothing about FastAPI, and being `async def`
# this isn't sent to the thread pool.
async def get_user_repository(db: DbDep) -> UserRepository:
    return UserRepository(db)


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]
