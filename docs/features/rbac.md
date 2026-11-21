# RBAC

`app/rbac/`: roles, permissions and the checks that use them. The design is
explained in
[../concepts/roles-and-permissions.md](../concepts/roles-and-permissions.md);
this page is about the code.

## What it owns

- The permission catalog, in code.
- The `roles` and `permissions` collections.
- The routes under `/api/v1/roles` and `/api/v1/permissions`.
- The logic behind `PUT /api/v1/users/{user_id}/roles`.
- The seed command, `app/scripts/seed.py`.

## The modules

| Module | Holds | Imports FastAPI? |
| ------ | ----- | ---------------- |
| `permissions.py` | `Perm`, descriptions, the built-in role definitions | No |
| `policy.py` | `Principal`, `authorize`, `ensure_can_grant`. A check that names no permission raises: it can only be a forgotten argument | No |
| `models.py`, `schemas.py` | `Role`, and what the API accepts and returns | No |
| `repository.py` | Queries on `roles` | No |
| `service.py` | `RoleService` | No |
| `sync.py` | What startup does | No |
| `providers.py` | The role repository's provider, kept free of `auth` | Yes |
| `dependencies.py` | `get_principal`, `get_optional_principal`, `require_permission`, the service provider, and `LOGIN_OPTIONAL` for marking a route as login-optional in the API docs | Yes |
| `router.py` | The routes | Yes |

`policy.py` is separate from `dependencies.py` so that services, which may not
import FastAPI, can still call `authorize`.

## The collections

`roles`:

| Field | Notes |
| ----- | ----- |
| `name` | Unique. 2 to 40 lowercase letters, digits or underscores |
| `description` | Up to 200 characters |
| `permissions` | Sorted, no duplicates, all from the catalog |
| `is_system` | True for `admin` and `reader` |
| `is_default` | True for exactly one role, enforced by a unique index |
| `lock_version` | A counter used only to make transactions conflict; never returned |
| `created_at`, `updated_at` | |

`permissions`: `_id` is the name, plus `resource` and `description`. Written
only by the startup sync. The API serves the catalog from the code, which the
sync keeps identical.

## The service

`RoleService`:

| Method | Rules it applies |
| ------ | ---------------- |
| `create_role` | Can't grant what you don't hold; name must be free |
| `update_role` | System roles keep their name; `admin` keeps its permissions and can't be the default; the default flag can be moved but not removed; added permissions must be held |
| `delete_role` | Not a system role, not the default; removed from its holders in the same transaction |
| `assign_roles` | Unknown ids are refused; roles added or removed must be within the caller's permissions; the last active admin stays |
| `ensure_another_active_admin` | The lock and the count, for any code that stops a user being an active admin |

## Using it from another feature

```python
# Something without an owner: guard the route.
@router.post("", dependencies=[Depends(require_permission(Perm.POSTS_CREATE))])

# Something with an owner: check in the service, after loading it.
authorize(principal, any_=Perm.POSTS_UPDATE_ANY, own=Perm.POSTS_UPDATE_OWN, owner_id=post.author_id)

# An action with no ":any" form: the owner, and nobody else.
authorize(principal, own=Perm.COMMENTS_UPDATE_OWN, owner_id=comment.author_id)

# A yes or no, for filtering.
if principal.can(any_=Perm.POSTS_READ_DRAFTS_ANY, owner_id=post.author_id): ...
```

## Rules that are easy to break

- **Anything that hands out permissions or roles calls `ensure_can_grant`.**
- **Anything that stops a user being an active admin calls
  `ensure_another_active_admin`, inside its transaction.**
- **Every query inside a transaction is passed `session=`.** One that isn't
  runs outside it, sees other data, and conflicts with nothing.
- **The lock must write something new each time.** Setting a field to the
  value it already has is no write, and locks nothing; hence a counter.
- **Clear the old default before setting the new one.** The unique index
  allows one default at a time, even in the middle of a transaction.

## Key decisions

- **Startup creates the system roles, not the seed command.** Sign-up needs a
  default role from the first request, whether or not anyone has seeded.
- **`reader` is created once and then left alone**, while `admin` is reset at
  every start. An administrator may have tuned `reader`; nobody should be
  able to weaken `admin`.
- **A deleted role's id may linger on a user** if the role is assigned at the
  very moment it is deleted. Such an id matches nothing and grants nothing,
  so the gap was accepted rather than locked away.
- **The seed refuses an existing email unless told `--promote`.** Sign-up is
  open, so the account may not be the operator's.

## Tests

`tests/rbac/test_roles.py` over HTTP, including the escalation and race tests;
`tests/rbac/test_rbac_sync.py` for startup; `tests/rbac/test_seed.py` and
`tests/rbac/test_seed_cli.py` for the command; unit tests in
`tests/rbac/test_rbac_rules.py` and `tests/rbac/test_role_schemas.py`.
