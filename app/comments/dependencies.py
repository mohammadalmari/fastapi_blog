from typing import Annotated

from fastapi import Depends

from app.comments.repository import CommentRepository
from app.comments.service import CommentService
from app.core.database import DbDep
from app.posts.dependencies import PostRepositoryDep
from app.users.dependencies import UserRepositoryDep


async def get_comment_repository(db: DbDep) -> CommentRepository:
    return CommentRepository(db)


CommentRepositoryDep = Annotated[CommentRepository, Depends(get_comment_repository)]


async def get_comment_service(
    db: DbDep, comments: CommentRepositoryDep, posts: PostRepositoryDep, users: UserRepositoryDep
) -> CommentService:
    return CommentService(db, comments, posts, users)


CommentServiceDep = Annotated[CommentService, Depends(get_comment_service)]
