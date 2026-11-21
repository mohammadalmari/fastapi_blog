# Roles and permissions

## What is this?

What a logged-in user is allowed to do. Three things, each built from the one
before:

- A **permission** is a fixed name in the code, such as `posts:update:own`.
- A **role** is a set of permissions. Administrators create and edit roles
  while the app runs.
- A **user** holds any number of roles and may do whatever any of them allows.

## Why this shape?

The code has to check for permissions by name, so they cannot be invented at
runtime: a permission nobody's code checks would do nothing. What changes
from one deployment to the next is who should hold which combination, and
that is what roles are for.

## Where the code is

| What | Where |
| ---- | ----- |
| The catalog (`Perm` enum, descriptions, built-in role definitions) | `app/rbac/permissions.py` |
| The rules (`Principal`, `authorize`, `ensure_can_grant`) | `app/rbac/policy.py` |
| Loading the caller and guarding routes | `app/rbac/dependencies.py` |
| Managing roles and assigning them | `app/rbac/service.py` |
| Startup sync | `app/rbac/sync.py` |
| The seed command | `app/scripts/seed.py` |

## How it works

### Naming

`resource:action`, plus `:own` or `:any` when the action is on something that
has an owner. `posts:update:own` lets you edit your posts; `posts:update:any`
anyone's.

### Checking a request

`get_principal` loads the user's roles on every request and adds up their
permissions into a `Principal`. Changing a role, or which roles someone has,
therefore takes effect on their next request.

- **Things without an owner** are guarded on the route with
  `require_permission(Perm.X)`. Without the permission the answer is 403.
- **Things with an owner** are checked in the service, once the thing is
  loaded: `authorize(principal, any_=..., own=..., owner_id=...)` passes with
  the `:any` permission, or with the `:own` one when the thing is the
  caller's. `principal.can(...)` is the same rule as a yes or no, for
  filtering a list.

### You can't grant what you don't hold

`ensure_can_grant` is applied wherever permissions or roles are handed out:

- Creating or editing a role: every permission you add must be one you hold.
- Assigning or removing a role: you must hold every permission it carries.

Without this rule, `roles:update` would be the permission to do anything: its
holder could write every permission into a role of their own. The same went
for `users:assign_roles`. A security review of the first version showed both
escalations working in three requests. With the rule, both permissions can be
given to someone who is not a full administrator.

Taking a permission *out* of a role needs no such check, since it hands
nobody anything.

### The built-in roles

| Role | Holds | Notes |
| ---- | ----- | ----- |
| `admin` | Every permission | System role. Reset to the full catalog at every startup |
| `reader` | Commenting and liking | System role. The default for new accounts |
| `author`, `moderator`, `editor` | See `OPTIONAL_ROLES` | Created by the seed command; ordinary roles, freely editable |

The two system roles are created at startup, so sign-up always finds a
default role.

### The rules that keep it administrable

| Rule | Enforced by |
| ---- | ----------- |
| System roles can't be renamed or deleted | `RoleService`, and the delete query's own conditions |
| `admin` can't lose permissions | `RoleService.update_role`; startup resets it regardless |
| Exactly one role is the default | A transaction, plus a unique index on `is_default: true` |
| `admin` can't be the default | `RoleService.update_role`; startup moves it back to `reader` |
| The last active admin can't be demoted | `RoleService.ensure_another_active_admin` |

The last rule needs care. "Is there another admin?" followed by "remove this
one" is two steps, and two requests demoting two different admins could each
pass the check before either wrote. The check runs in a transaction that
first writes to the admin role document. Two transactions writing the same
document cannot both commit, so MongoDB makes them take turns.

### Startup sync

`sync_rbac` runs in the app's lifespan. It stores the catalog in the
`permissions` collection, removes permissions that have left the code from
there and from every role, and makes sure the system roles and a default
exist.

Removal is permanent. Starting an *older* version of the code against a
database a newer one has used strips the newer permissions from every role,
and they do not come back on upgrade. The sync logs a warning when it removes
anything.

### The seed command

```bash
uv run python -m app.scripts.seed --email you@example.com --username you
```

Creates the optional roles and the first admin, asking for a password. If the
email already has an account it refuses: sign-up is open and emails aren't
verified, so that account may belong to someone who registered the address
first. `--promote` gives an existing account the admin role, on purpose and
by name.

## What a first-time reader should know

- A new permission is added in one place, the `Perm` enum with its
  description. Startup does the rest, and `admin` gains it automatically.
- Anything that stops a user being an active admin must call
  `ensure_another_active_admin` inside its transaction. Today that is role
  assignment; deactivating users will need it too.
- A role id on a user that matches no role is ignored. It grants nothing.
- An admin can still give the default role any permission they hold. Only the
  `admin` role itself is barred from being the default.
