# Users

`app/users/`: the user as stored, profiles, avatars, and the routes about
users.

## What it owns

- The `users` collection, through `UserRepository`.
- The internal `User` model and the public schemas.
- The routes under `/api/v1/users`, and `GET /media/avatars/{avatar_key}`.
  (`GET /api/v1/users/me/posts` is in the posts feature.)

Two routes under `/users` have their logic elsewhere, because of what they
are about: changing a password is in [auth.md](auth.md), and roles and
account status are in [rbac.md](rbac.md). Signing up and logging in are in
auth too. This feature stores the result.

## The modules

| Module | Holds |
| ------ | ----- |
| `models.py` | `User`, as the app handles it internally |
| `schemas.py` | What the API accepts and returns; how emails, usernames and profile text are checked |
| `repository.py` | Every query on `users`, and its indexes |
| `service.py` | `UserService`: profiles, the list of accounts, avatars |
| `avatars.py` | `make_avatar`: an upload in, a small WebP out. Pure, no app |
| `dependencies.py` | Providers for the repository and the service |
| `router.py` | The `/users` routes and the avatar file route |

## The collection

`users`:

| Field | Notes |
| ----- | ----- |
| `_id` | ObjectId |
| `email` | Unique. Stored lowercased and Unicode-normalized. Not editable |
| `username` | Unique. 3 to 30 lowercase letters, digits or underscores |
| `password_hash` | Argon2id |
| `display_name` | Up to 50 characters, one line, or null |
| `bio` | Up to 500 characters, line breaks allowed, or null |
| `avatar_key` | The avatar's file name, or null |
| `role_ids` | The roles the user holds |
| `is_active` | False for a deactivated account |
| `created_at`, `updated_at` | UTC, to the millisecond |

Indexes: unique on `email` and on `username`; `users_newest` and
`users_by_status` for the administrators' list.

## Six shapes of a user

| Model | Used for | Shows the email? |
| ----- | -------- | ---------------- |
| `User` (`models.py`) | Inside the app; has `password_hash` | |
| `UserAccount` | The response to sign-up | Yes, their own |
| `UserMe` | The caller's own account, with roles and permissions | Yes, their own |
| `UserAdmin` | What an administrator sees | Yes |
| `UserPublic` | A public profile | No |
| `AuthorSummary` | Beside something the user wrote | No |

A route returns the internal `User`, or a dict built from it, and declares the
public schema as its `response_model`. FastAPI then keeps only that schema's
fields at every level, which is what stops `password_hash` reaching a
response. Every shape turns the stored `avatar_key` into `avatar_url`.

## Rules

- **Emails and usernames are normalized the same way at sign-up and at
  login.** Otherwise `José@…` typed with a combining accent would register and
  then never log in. `normalize_login_identifier` is the one place.
- **Uniqueness is the index's job.** Creating an account and changing a
  username both write first and translate the duplicate-key error, rather
  than look the name up beforehand.
- **A username has no "@"**, which is how login tells it from an email.
- **Public routes look users up with `get_by_username`, never
  `get_by_login`.** The second also matches emails, which are private; a
  public route using it would reveal which emails have accounts.
- **The profile edit refuses fields it doesn't have.** An email, a role, a
  password or `is_active` in the body is a 422, not silently dropped: the
  classic mistake is copying whatever the client sent onto the user.
- **A deactivated account has no profile page**: 404, like a name nobody
  has. What its owner wrote stays, under their name.
- **Text that will be displayed refuses invisible characters** (see
  `app/core/text.py`), so a display name can't be made to read backwards or
  look like someone else's.

## Avatars

`PUT /users/me/avatar` takes one file, the form field `file`, and nothing
else: a second file or a stray text field is a 400.

- **Whatever is uploaded is decoded and drawn again.** `make_avatar` opens it
  with Pillow, restricted to PNG, JPEG and WebP, crops it to a square and
  saves a 256-pixel WebP. Nothing of the original file is stored. That drops
  its metadata (a phone photo carries where it was taken) and anything
  hidden in it (a file can be a valid image and something else at once).
- **Three limits, checked in this order.** The route's own body limit
  (2 MB plus room for the form) stops a huge request while it arrives. The
  file itself may be at most 2 MB (413). And the picture may have only so
  many pixels, checked from the header before any pixel is decoded.
- **The pixel limit is about memory, and depends on the format.** A few
  hundred bytes of one flat colour can declare an enormous picture. A JPEG is
  decoded at a fraction of its size, so it may be 4096 x 4096 (about 50 MiB
  to decode). PNG and WebP are decoded whole and may be 2048 x 2048 (about
  70 MiB). Before the review there was one limit, and a WebP of under a
  kilobyte cost about 460 MiB.
- **The declared content type and the file name are ignored.** Only what the
  bytes decode to counts.
- **A damaged file is a 422, never a crash.** A decoder fed nonsense fails in
  many ways; all of them are caught. The EXIF block is read for one number,
  which way up the picture is, and never rewritten: rewriting a damaged one
  was the crash the review found.
- **Decoding runs in a thread, two at a time**, like password hashing.
- **Every upload gets a new random name.** It can't be guessed from the
  user, and because a name is never reused, the file route can tell caches
  to keep a picture forever. `new_avatar_key` makes the names and
  `AVATAR_KEY_PATTERN` describes them; both are in `avatars.py`.
- **The file first, then the pointer, then the old file away.** At no moment
  does an account point at a file that isn't there. Setting the new name and
  reading the old one is a single database operation, so two uploads at once
  each remove a different old file.
- **`GET /media/avatars/{avatar_key}` serves avatars and nothing else.** The
  name must match the pattern, so `../` never reaches the storage, and
  neither does another file that happens to be in the folder. `LocalStorage`
  refuses any key that resolves outside its folder, as a second line.

Files go through the `Storage` protocol in `app/core/storage.py`. The one
implementation, `LocalStorage`, keeps them in `FASTAPI_MEDIA_DIR` (default
`./media`). That suits one server with a disk that outlives it; with several
servers, write another class for an object store and swap it in
`create_app`.

## Key decisions

- **`User` is the read schema for every stored user**, old ones included. A
  new field needs a default or a backfill.
- **A changed username frees the old one at once.** Links to the old profile
  break, and someone else can take the name. Accepted for now; holding a
  name back for a while needs a history of names.
- **An empty string clears a display name or bio; null means "not sent".**
  The same convention as editing a post.
- **Import direction.** `auth` and `rbac` import from `users`. `users` imports
  from them only in its router, and only their `dependencies` modules plus
  modules that import nothing back (`rbac.permissions`, `rbac.policy`). That
  is why `UserService` knows nothing of passwords, roles or status.
- **The list of accounts gets its roles in the router**, from
  `RoleService.roles_of`: one query for the page.

## Known limits

- A crash between storing a file and pointing the account at it leaves a
  file nothing points at. Nothing sweeps them up yet.
- A removed or replaced avatar stays in browser and proxy caches for up to a
  year, because its address is marked as never changing. Avatars of
  deactivated accounts stay served, too.
- Colour profiles are dropped without converting to sRGB, so a photo taken
  in a wider colour space looks slightly duller.
- An unwritable media folder shows as an error on the first upload, not at
  startup.
- No rate limit on uploads yet.

## Tests

`tests/users/test_users.py` (the account and its roles), `test_profiles.py`,
`test_accounts.py` (the list, deactivating), `test_avatars.py` over HTTP;
`test_user_schemas.py` and `test_avatar_rules.py` for the rules, without an
app; `tests/core/test_storage.py` for `LocalStorage`.
