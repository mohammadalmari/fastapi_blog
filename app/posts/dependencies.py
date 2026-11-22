from typing import Annotated, cast

from fastapi import Depends, Path, Request

from app.comments.repository import CommentRepository
from app.core.database import DbDep
from app.core.ttl import Remembered
from app.core.types import PyObjectId
from app.likes.repository import LikeRepository
from app.posts.models import TagCount
from app.posts.repository import PostRepository
from app.posts.service import PostService
from app.users.dependencies import UserRepositoryDep

# Shared with the features whose routes sit under a post, such as comments.
PostId = Annotated[PyObjectId, Path(description="The post's id.")]


async def get_post_repository(db: DbDep) -> PostRepository:
    return PostRepository(db)


PostRepositoryDep = Annotated[PostRepository, Depends(get_post_repository)]


async def get_tag_cache(request: Request) -> Remembered[list[TagCount]]:
    """The app's one remembered list of tags (see ``app.main``)."""
    return cast(Remembered[list[TagCount]], request.app.state.tag_cache)


async def get_post_service(
    db: DbDep,
    posts: PostRepositoryDep,
    users: UserRepositoryDep,
    tag_cache: Annotated[Remembered[list[TagCount]], Depends(get_tag_cache)],
) -> PostService:
    # Here is where "deleting a post deletes its comments and likes" is wired
    # up, and where posts learn which of them the caller has liked. The other
    # features' repositories are built directly, not injected from their
    # `dependencies` modules: those depend on posts and import this module.
    likes = LikeRepository(db)
    return PostService(
        db,
        posts,
        users,
        on_delete=[CommentRepository(db).delete_for_post, likes.delete_for_post],
        liked=likes.liked_posts,
        tag_cache=tag_cache,
    )


PostServiceDep = Annotated[PostService, Depends(get_post_service)]
