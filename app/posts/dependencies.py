from typing import Annotated

from fastapi import Depends

from app.core.database import DbDep
from app.posts.repository import PostRepository
from app.posts.service import PostService
from app.users.dependencies import UserRepositoryDep


async def get_post_repository(db: DbDep) -> PostRepository:
    return PostRepository(db)


async def get_post_service(
    posts: Annotated[PostRepository, Depends(get_post_repository)], users: UserRepositoryDep
) -> PostService:
    return PostService(posts, users)


PostServiceDep = Annotated[PostService, Depends(get_post_service)]
