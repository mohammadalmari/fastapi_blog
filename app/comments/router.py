from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Response, status

from app.comments.dependencies import CommentServiceDep
from app.comments.models import CommentView
from app.comments.schemas import CommentCreate, CommentPublic, CommentUpdate
from app.core.errors import error_responses
from app.core.pagination import Page, PageParamsDep
from app.core.types import PyObjectId
from app.posts.dependencies import PostId
from app.rbac.dependencies import (
    LOGIN_OPTIONAL,
    OptionalPrincipalDep,
    PrincipalDep,
    require_permission,
)
from app.rbac.permissions import Perm
from app.rbac.policy import Principal

# A post's comments sit under the post in the URL; a single comment is
# addressed by its own id.
post_comments_router = APIRouter(prefix="/posts/{post_id}/comments", tags=["comments"])
comments_router = APIRouter(prefix="/comments", tags=["comments"])

CommentId = Annotated[PyObjectId, Path(description="The comment's id.")]
# As a parameter, because the routes need to know who is commenting.
CommenterDep = Annotated[Principal, Depends(require_permission(Perm.COMMENTS_CREATE))]

_READ_ERRORS = error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_404_NOT_FOUND)
_WRITE_ERRORS = error_responses(
    status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND
)
_CREATE_ERRORS = error_responses(
    status.HTTP_401_UNAUTHORIZED,
    status.HTTP_403_FORBIDDEN,
    status.HTTP_404_NOT_FOUND,
    status.HTTP_409_CONFLICT,
)


def _body(view: CommentView) -> dict[str, Any]:
    """
    The comment and its author go in whole and `response_model` keeps the
    public fields. A placeholder's content is stored empty and sent as null,
    so a client can tell "erased" from "said nothing".
    """
    comment = view.comment
    content = None if comment.is_deleted else comment.content
    return {**comment.model_dump(), "content": content, "author": view.author}


def _page(views: list[CommentView], next_cursor: str | None) -> dict[str, Any]:
    return {"items": [_body(view) for view in views], "next_cursor": next_cursor}


def _never_cache_for(principal: Principal | None, response: Response) -> None:
    if principal is not None:
        # What a logged-in caller sees may differ (the comments of their own
        # draft), so no cache may keep it and hand it to someone else.
        response.headers["Cache-Control"] = "private, no-store"


@post_comments_router.get(
    "",
    response_model=Page[CommentPublic],
    responses=_READ_ERRORS,
    openapi_extra=LOGIN_OPTIONAL,
)
async def list_comments(
    post_id: PostId,
    params: PageParamsDep,
    principal: OptionalPrincipalDep,
    service: CommentServiceDep,
    response: Response,
) -> dict[str, Any]:
    """
    The comments on a post, oldest first, each with its number of replies.
    Replies themselves are listed per comment. Follow `next_cursor` for more.
    """
    views, next_cursor = await service.list_for_post(
        principal, post_id, after=params.after(), limit=params.limit
    )
    _never_cache_for(principal, response)
    return _page(views, next_cursor)


@post_comments_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=CommentPublic,
    responses=_CREATE_ERRORS,
)
async def create_comment(
    post_id: PostId, data: CommentCreate, principal: CommenterDep, service: CommentServiceDep
) -> dict[str, Any]:
    """Comment on a published post."""
    return _body(await service.create(principal, post_id, data))


@comments_router.get(
    "/{comment_id}/replies",
    response_model=Page[CommentPublic],
    responses=_READ_ERRORS,
    openapi_extra=LOGIN_OPTIONAL,
)
async def list_replies(
    comment_id: CommentId,
    params: PageParamsDep,
    principal: OptionalPrincipalDep,
    service: CommentServiceDep,
    response: Response,
) -> dict[str, Any]:
    """The replies to a comment, oldest first."""
    views, next_cursor = await service.list_replies(
        principal, comment_id, after=params.after(), limit=params.limit
    )
    _never_cache_for(principal, response)
    return _page(views, next_cursor)


@comments_router.post(
    "/{comment_id}/replies",
    status_code=status.HTTP_201_CREATED,
    response_model=CommentPublic,
    responses=_CREATE_ERRORS,
)
async def create_reply(
    comment_id: CommentId, data: CommentCreate, principal: CommenterDep, service: CommentServiceDep
) -> dict[str, Any]:
    """Reply to a comment. Replies can't themselves be replied to."""
    return _body(await service.reply(principal, comment_id, data))


@comments_router.patch("/{comment_id}", response_model=CommentPublic, responses=_WRITE_ERRORS)
async def update_comment(
    comment_id: CommentId, data: CommentUpdate, principal: PrincipalDep, service: CommentServiceDep
) -> dict[str, Any]:
    """Edit your own comment. Nobody can edit someone else's."""
    return _body(await service.update(principal, comment_id, data))


@comments_router.delete(
    "/{comment_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_WRITE_ERRORS
)
async def delete_comment(
    comment_id: CommentId, principal: PrincipalDep, service: CommentServiceDep
) -> None:
    """
    Delete a comment. It is removed, unless it has replies: then what it said
    is erased and it stays listed as a placeholder, so they keep their place.
    """
    await service.delete(principal, comment_id)
