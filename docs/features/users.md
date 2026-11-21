# Users

`app/users/`: the user as stored, and the routes about users.

## What it owns

- The `users` collection, through `UserRepository`.
- The internal `User` model and the public schemas.
- `GET /api/v1/users/me` and `PUT /api/v1/users/{user_id}/roles`.
  (`GET /api/v1/users/me/posts` is in the posts feature.)

Signing up and logging in are in [auth.md](auth.md); deciding which roles a
user may be given is in [rbac.md](rbac.md). This feature stores the result.

## The collection

`users`:

| Field | Notes |
| ----- | ----- |
| `_id` | ObjectId |
| `email` | Unique. Stored lowercased and Unicode-normalized |
| `username` | Unique. 3 to 30 lowercase letters, digits or underscores |
| `password_hash` | Argon2id |
| `display_name`, `bio`, `avatar_key` | Profile fields, not editable yet |
| `role_ids` | The roles the user holds |
| `is_active` | False for a deactivated account |
| `created_at`, `updated_at` | UTC, to the millisecond |

## Four shapes of a user

| Model | Used for | Has `password_hash`? |
| ----- | -------- | -------------------- |
| `User` (`models.py`) | Inside the app | Yes |
| `UserAccount` (`schemas.py`) | The response to sign-up | No |
| `UserMe` | `GET /users/me`: the account plus roles and permissions | No |
| `UserAdmin` | What an administrator sees after assigning roles | No |

A route returns the internal `User`, or a dict built from it, and declares the
public schema as its `response_model`. FastAPI then keeps only that schema's
fields at every level, which is what stops `password_hash` reaching a
response.

## Rules

- **Emails and usernames are normalized the same way at sign-up and at
  login.** Otherwise `José@…` typed with a combining accent would register and
  then never log in. `normalize_login_identifier` is the one place.
- **Uniqueness is the index's job.** `create` inserts and translates the
  duplicate-key error into `DuplicateUserError`, which says which field.
- **A username has no "@"**, which is how login tells it from an email.
- **Public routes look users up with `get_by_username`, never
  `get_by_login`.** The second also matches emails, which are private; a
  public route using it would reveal which emails have accounts.
- **`get_many` fetches a page's users in one query**, for lists that show an
  author beside each item.

## Key decisions

- **`User` is the read schema for every stored user**, old ones included. A
  new field needs a default or a backfill.
- **Role assignment's route is here, its logic in `rbac`.** The URL is about a
  user; the rules are about roles.
- **Import direction.** `auth` and `rbac` import from `users`. `users` imports
  from them only in its router, and only their `dependencies` modules plus
  modules that import nothing back (`rbac.permissions`, `rbac.policy`).

## Not here yet

Editing a profile, changing a password, listing users and deactivating them
are planned for the account-management milestone. Deactivation will have to
apply the last-active-admin rule: see [rbac.md](rbac.md).

## Tests

`tests/users/test_users.py` over HTTP, and `tests/users/test_user_schemas.py` for the
normalization and validation rules.
