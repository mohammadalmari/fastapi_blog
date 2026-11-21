"""
The permission catalog: every permission that exists, defined here in code.

Code checks for these by name, so they can't be invented at runtime; what
administrators create and edit at runtime are *roles*, which are sets of them.
At startup the catalog is copied to the database (see ``sync``).

Naming: ``resource:action``, plus ``:own`` or ``:any`` when the action is on
something that has an owner. ``posts:update:own`` lets you edit your posts,
``posts:update:any`` anyone's.
"""

from dataclasses import dataclass
from enum import StrEnum


class Perm(StrEnum):
    POSTS_CREATE = "posts:create"
    POSTS_UPDATE_OWN = "posts:update:own"
    POSTS_UPDATE_ANY = "posts:update:any"
    POSTS_DELETE_OWN = "posts:delete:own"
    POSTS_DELETE_ANY = "posts:delete:any"
    POSTS_PUBLISH_OWN = "posts:publish:own"
    POSTS_PUBLISH_ANY = "posts:publish:any"
    POSTS_READ_DRAFTS_ANY = "posts:read_drafts:any"

    COMMENTS_CREATE = "comments:create"
    COMMENTS_UPDATE_OWN = "comments:update:own"
    COMMENTS_DELETE_OWN = "comments:delete:own"
    COMMENTS_DELETE_ANY = "comments:delete:any"

    LIKES_CREATE = "likes:create"

    USERS_READ_ANY = "users:read:any"
    USERS_DEACTIVATE = "users:deactivate"
    USERS_ASSIGN_ROLES = "users:assign_roles"

    ROLES_READ = "roles:read"
    ROLES_CREATE = "roles:create"
    ROLES_UPDATE = "roles:update"
    ROLES_DELETE = "roles:delete"

    @property
    def resource(self) -> str:
        return self.value.split(":")[0]


DESCRIPTIONS: dict[Perm, str] = {
    Perm.POSTS_CREATE: "Write new posts",
    Perm.POSTS_UPDATE_OWN: "Edit your own posts",
    Perm.POSTS_UPDATE_ANY: "Edit anyone's posts",
    Perm.POSTS_DELETE_OWN: "Delete your own posts",
    Perm.POSTS_DELETE_ANY: "Delete anyone's posts",
    Perm.POSTS_PUBLISH_OWN: "Publish and unpublish your own posts",
    Perm.POSTS_PUBLISH_ANY: "Publish and unpublish anyone's posts",
    Perm.POSTS_READ_DRAFTS_ANY: "Read anyone's drafts",
    Perm.COMMENTS_CREATE: "Comment on posts",
    Perm.COMMENTS_UPDATE_OWN: "Edit your own comments",
    Perm.COMMENTS_DELETE_OWN: "Delete your own comments",
    Perm.COMMENTS_DELETE_ANY: "Delete anyone's comments",
    Perm.LIKES_CREATE: "Like posts and comments",
    Perm.USERS_READ_ANY: "List users and see their accounts",
    Perm.USERS_DEACTIVATE: "Deactivate and reactivate users",
    Perm.USERS_ASSIGN_ROLES: "Change which roles a user has",
    Perm.ROLES_READ: "See roles and permissions",
    Perm.ROLES_CREATE: "Create roles",
    Perm.ROLES_UPDATE: "Edit roles",
    Perm.ROLES_DELETE: "Delete roles",
}

# --- built-in roles ---------------------------------------------------------

# The two system roles. The app depends on them, so startup makes sure they
# exist and they can't be deleted or renamed.
ADMIN_ROLE = "admin"  # always holds every permission
READER_ROLE = "reader"  # what a new account gets, unless another role is made the default

READER_PERMISSIONS: tuple[Perm, ...] = (
    Perm.COMMENTS_CREATE,
    Perm.COMMENTS_UPDATE_OWN,
    Perm.COMMENTS_DELETE_OWN,
    Perm.LIKES_CREATE,
)


@dataclass(frozen=True)
class RoleDefinition:
    description: str
    permissions: tuple[Perm, ...]


# Ordinary roles the seed command creates as a starting point. Administrators
# can edit or delete them like any role they made themselves.
OPTIONAL_ROLES: dict[str, RoleDefinition] = {
    "author": RoleDefinition(
        "Writes and publishes their own posts",
        (
            *READER_PERMISSIONS,
            Perm.POSTS_CREATE,
            Perm.POSTS_UPDATE_OWN,
            Perm.POSTS_DELETE_OWN,
            Perm.POSTS_PUBLISH_OWN,
        ),
    ),
    "moderator": RoleDefinition(
        "Removes comments and can read drafts",
        (Perm.COMMENTS_DELETE_ANY, Perm.POSTS_READ_DRAFTS_ANY),
    ),
    "editor": RoleDefinition(
        "Edits, publishes and deletes anyone's posts",
        (
            Perm.POSTS_UPDATE_ANY,
            Perm.POSTS_PUBLISH_ANY,
            Perm.POSTS_DELETE_ANY,
            Perm.POSTS_READ_DRAFTS_ANY,
        ),
    ),
}
