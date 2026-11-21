from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.comments.dependencies import CommentId
from app.core.errors import error_responses
from app.likes.dependencies import LikeServiceDep
from app.posts.dependencies import PostId
from app.rbac.dependencies import PrincipalDep, require_permission
from app.rbac.permissions import Perm
from app.rbac.policy import Principal

# No prefix: a like is addressed through the thing it is on.
router = APIRouter(tags=["likes"])

# As a parameter, because the routes need to know who is liking.
LikerDep = Annotated[Principal, Depends(require_permission(Perm.LIKES_CREATE))]

# PUT and DELETE, not a "toggle": each says what the result should be, so a
# request sent twice (a double click, a retry after a timeout) ends in the
# same place as one sent once.
_LIKE_ERRORS = error_responses(
    status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND
)
# Taking a like back has one answer, whatever the id: see LikeService._unlike.
_UNLIKE_ERRORS = error_responses(status.HTTP_401_UNAUTHORIZED)


@router.put("/posts/{post_id}/like", status_code=status.HTTP_204_NO_CONTENT, responses=_LIKE_ERRORS)
async def like_post(post_id: PostId, principal: LikerDep, service: LikeServiceDep) -> None:
    """Like a post. Liking one you already like changes nothing."""
    await service.like_post(principal, post_id)


@router.delete(
    "/posts/{post_id}/like", status_code=status.HTTP_204_NO_CONTENT, responses=_UNLIKE_ERRORS
)
async def unlike_post(post_id: PostId, principal: PrincipalDep, service: LikeServiceDep) -> None:
    """
    Take your like back. Needs no permission. The answer is the same whether
    or not you had one, and whether or not there is such a thing to like.
    """
    await service.unlike_post(principal, post_id)


@router.put(
    "/comments/{comment_id}/like", status_code=status.HTTP_204_NO_CONTENT, responses=_LIKE_ERRORS
)
async def like_comment(comment_id: CommentId, principal: LikerDep, service: LikeServiceDep) -> None:
    """Like a comment. Liking one you already like changes nothing."""
    await service.like_comment(principal, comment_id)


@router.delete(
    "/comments/{comment_id}/like",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_UNLIKE_ERRORS,
)
async def unlike_comment(
    comment_id: CommentId, principal: PrincipalDep, service: LikeServiceDep
) -> None:
    """
    Take your like back. Needs no permission. The answer is the same whether
    or not you had one, and whether or not there is such a thing to like.
    """
    await service.unlike_comment(principal, comment_id)
