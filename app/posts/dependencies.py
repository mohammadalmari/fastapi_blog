from typing import Annotated

from fastapi import Depends, Path

from app.comments.repository import CommentRepository
from app.core.database import DbDep
from app.core.types import PyObjectId
from app.posts.repository import PostRepository
from app.posts.service import PostService
from app.users.dependencies import UserRepositoryDep

# Shared with the features whose routes sit under a post, such as comments.
PostId = Annotated[PyObjectId, Path(description="The post's id.")]


async def get_post_repository(db: DbDep) -> PostRepository:
    return PostRepository(db)


PostRepositoryDep = Annotated[PostRepository, Depends(get_post_repository)]


async def get_post_service(
    db: DbDep, posts: PostRepositoryDep, users: UserRepositoryDep
) -> PostService:
    # Here is where "deleting a post deletes its comments" is wired up. The
    # comment repository is built directly, not injected from the comments
    # feature: comments depends on posts, and its `dependencies` module
    # imports this one.
    return PostService(db, posts, users, on_delete=[CommentRepository(db).delete_for_post])


PostServiceDep = Annotated[PostService, Depends(get_post_service)]
