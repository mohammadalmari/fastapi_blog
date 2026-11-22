from typing import Annotated

from fastapi import Depends

from app.core.database import DbDep
from app.core.storage import StorageDep
from app.users.repository import UserRepository
from app.users.service import UserService


# A provider function rather than `Depends(UserRepository)`: the repository
# stays a plain class that knows nothing about FastAPI, and being `async def`
# this isn't sent to the thread pool.
async def get_user_repository(db: DbDep) -> UserRepository:
    return UserRepository(db)


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]


async def get_user_service(users: UserRepositoryDep, storage: StorageDep) -> UserService:
    return UserService(users, storage)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]
