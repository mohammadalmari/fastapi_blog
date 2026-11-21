from typing import Annotated

from fastapi import Depends

from app.comments.dependencies import CommentRepositoryDep
from app.core.database import DbDep
from app.likes.repository import LikeRepository
from app.likes.service import LikeService
from app.posts.dependencies import PostRepositoryDep


async def get_like_repository(db: DbDep) -> LikeRepository:
    return LikeRepository(db)


LikeRepositoryDep = Annotated[LikeRepository, Depends(get_like_repository)]


async def get_like_service(
    db: DbDep, likes: LikeRepositoryDep, posts: PostRepositoryDep, comments: CommentRepositoryDep
) -> LikeService:
    return LikeService(db, likes, posts, comments)


LikeServiceDep = Annotated[LikeService, Depends(get_like_service)]
