"""
Who may see a post. Plain Python, so that features built on posts (comments,
later likes) can ask without depending on the posts service.
"""

from app.posts.models import Post
from app.rbac.permissions import Perm
from app.rbac.policy import Principal

# Any of these lets its holder see other people's drafts. Reading them is its
# own permission, but so is each way of acting on them: someone trusted to
# publish any post has to be able to see the one they are publishing.
DRAFT_ACCESS = (
    Perm.POSTS_READ_DRAFTS_ANY,
    Perm.POSTS_UPDATE_ANY,
    Perm.POSTS_PUBLISH_ANY,
    Perm.POSTS_DELETE_ANY,
)


def may_see(principal: Principal | None, post: Post) -> bool:
    """
    Everyone sees a published post. A draft: its author, and whoever may act
    on drafts.

    What hangs from a post is exactly as visible as the post. A feature that
    shows or changes such things asks this first, and answers "not found"
    when it is False: a refusal would confirm that the post is there.
    """
    return post.status == "published" or may_see_unpublished(principal, post)


def may_see_unpublished(principal: Principal | None, post: Post) -> bool:
    """Whether the post would be visible to them as a draft, whatever it is now."""
    if principal is None:
        return False
    if post.author_id == principal.user.id:
        return True
    return any(principal.has(permission) for permission in DRAFT_ACCESS)
