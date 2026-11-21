from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.core.caching import never_cache, varies_by_login
from app.core.errors import error_responses
from app.core.pagination import Page
from app.posts.dependencies import PostId, PostServiceDep
from app.posts.models import PostView
from app.posts.repository import TAG_LIMIT
from app.posts.schemas import (
    FeedParams,
    MyPostsParams,
    PostCreate,
    PostPublic,
    PostSummary,
    PostUpdate,
    TagCount,
)
from app.rbac.dependencies import (
    LOGIN_OPTIONAL,
    OptionalPrincipalDep,
    PrincipalDep,
    require_permission,
)
from app.rbac.permissions import Perm
from app.rbac.policy import Principal

posts_router = APIRouter(prefix="/posts", tags=["posts"])
# The caller's own list sits under /users/me in the URL but is about posts.
my_posts_router = APIRouter(prefix="/users/me/posts", tags=["posts"])
tags_router = APIRouter(prefix="/tags", tags=["posts"])

# Reading is by slug and writing by id, so the paths /posts/{slug} and
# /posts/{post_id} differ only in the parameter's name. FastAPI routes them by
# method without trouble. Strict OpenAPI tooling may complain about the pair;
# that is accepted, in exchange for readable URLs on the route people share.
_WRITE_ERRORS = error_responses(
    status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND
)


# The internal post and its author go in whole; `response_model` keeps only the
# public fields at every level, so the author is cut down to id and names.
def _body(view: PostView) -> dict[str, Any]:
    return {**view.post.model_dump(), "author": view.author, "liked_by_me": view.liked_by_me}


def _page(views: list[PostView], next_cursor: str | None) -> dict[str, Any]:
    return {"items": [_body(view) for view in views], "next_cursor": next_cursor}


@posts_router.get(
    "",
    response_model=Page[PostSummary],
    responses=error_responses(status.HTTP_401_UNAUTHORIZED),
    openapi_extra=LOGIN_OPTIONAL,
)
async def list_posts(
    params: Annotated[FeedParams, Query()],
    principal: OptionalPrincipalDep,
    service: PostServiceDep,
    response: Response,
) -> dict[str, Any]:
    """
    Published posts, newest first by first publication. Follow `next_cursor`
    for the next page.

    A post that was unpublished and published again keeps its original place,
    so it can reappear among older posts rather than at the top.

    No login is needed. With one, `liked_by_me` says which posts you liked.
    """
    views, next_cursor = await service.feed(
        principal, tag=params.tag, author=params.author, after=params.after(), limit=params.limit
    )
    varies_by_login(response, logged_in=principal is not None)
    return _page(views, next_cursor)


@posts_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=PostPublic,
    responses=error_responses(
        status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN, status.HTTP_409_CONFLICT
    ),
)
async def create_post(
    data: PostCreate,
    principal: Annotated[Principal, Depends(require_permission(Perm.POSTS_CREATE))],
    service: PostServiceDep,
) -> dict[str, Any]:
    """Write a post. It starts as a draft, visible to you; publish it when it is ready."""
    return _body(await service.create(principal, data))


@posts_router.get(
    "/{slug}",
    response_model=PostPublic,
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_404_NOT_FOUND),
    openapi_extra=LOGIN_OPTIONAL,
)
async def read_post(
    slug: Annotated[str, Path(max_length=120, description="The post's slug, from its URL.")],
    principal: OptionalPrincipalDep,
    service: PostServiceDep,
    response: Response,
) -> dict[str, Any]:
    """
    One post. Published posts need no login. A draft is shown to its author
    and to those who may act on drafts, and is "not found" for everyone else.
    """
    view = await service.get_by_slug(principal, slug)
    varies_by_login(response, logged_in=principal is not None)
    if view.post.status == "draft":
        never_cache(response)  # shown to some; nobody else may be handed it
    return _body(view)


@posts_router.patch(
    "/{post_id}",
    response_model=PostPublic,
    responses=_WRITE_ERRORS,
)
async def update_post(
    post_id: PostId, data: PostUpdate, principal: PrincipalDep, service: PostServiceDep
) -> dict[str, Any]:
    """
    Change the fields sent; a field left out or sent as null stays as it is.

    The slug never changes, so links to the post keep working. Editing a post
    that is already published also needs the permission to publish it.
    """
    return _body(await service.update(principal, post_id, data))


@posts_router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_WRITE_ERRORS)
async def delete_post(post_id: PostId, principal: PrincipalDep, service: PostServiceDep) -> None:
    """Delete a post for good."""
    await service.delete(principal, post_id)


@posts_router.post("/{post_id}/publish", response_model=PostPublic, responses=_WRITE_ERRORS)
async def publish_post(
    post_id: PostId, principal: PrincipalDep, service: PostServiceDep
) -> dict[str, Any]:
    """Make a post public. Publishing one that already is changes nothing."""
    return _body(await service.set_published(principal, post_id, published=True))


@posts_router.post("/{post_id}/unpublish", response_model=PostPublic, responses=_WRITE_ERRORS)
async def unpublish_post(
    post_id: PostId, principal: PrincipalDep, service: PostServiceDep
) -> dict[str, Any]:
    """Turn a post back into a draft. Its first publication date is kept."""
    return _body(await service.set_published(principal, post_id, published=False))


@my_posts_router.get(
    "", response_model=Page[PostSummary], responses=error_responses(status.HTTP_401_UNAUTHORIZED)
)
async def list_my_posts(
    params: Annotated[MyPostsParams, Query()],
    principal: PrincipalDep,
    service: PostServiceDep,
    response: Response,
) -> dict[str, Any]:
    """Your own posts, drafts included, most recently created first."""
    never_cache(response)
    views, next_cursor = await service.mine(
        principal, status=params.status, after=params.after(), limit=params.limit
    )
    return _page(views, next_cursor)


@tags_router.get("", description=f"The {TAG_LIMIT} most used tags on published posts.")
async def list_tags(service: PostServiceDep) -> list[TagCount]:
    return [TagCount(name=tag.name, count=tag.count) for tag in await service.tags()]
