# Authentication

## What is this?

How the API knows who is calling. A user logs in once with an email or
username and a password, and from then on proves who they are with tokens:

| Token | What it is | Lives | Sent as |
| ----- | ---------- | ----- | ------- |
| Access token | A signed JWT naming the user | 15 minutes | `Authorization: Bearer <token>` on every request |
| Refresh token | A long random string | 14 days | An HttpOnly cookie, to the `/api/v1/auth` routes only |

The access token is what routes check. The refresh token only exists to get a
new access token without asking for the password again.

## Why two tokens?

Each has a weakness the other covers.

- A JWT is checked by its signature alone, with no database lookup, which
  makes it cheap. It also makes it impossible to revoke: until it expires it
  is valid. So it is kept short-lived.
- A random string means nothing by itself; the server looks it up. That makes
  it revocable, and it is the thing that "log out" actually ends. It is
  long-lived, so it is kept where page JavaScript cannot read it.

## Where the code is

| What | Where |
| ---- | ----- |
| Creating and decoding tokens | `app/core/security/tokens.py` |
| Register, login, refresh, logout | `app/auth/service.py`, routes in `app/auth/router.py` |
| Stored refresh tokens | `app/auth/repository.py` (collection `refresh_tokens`) |
| "Who is the current user?" | `get_current_user` in `app/auth/dependencies.py` |
| The Origin check | `verify_origin` in `app/auth/dependencies.py` |

## How it works

### Login

`POST /auth/login` takes an OAuth2 password form (`username`, `password`),
which is what the "Authorize" button in `/docs` sends. The `username` field
accepts an email or a username.

- The response body carries the access token.
- The refresh token is set as a cookie: `HttpOnly`, `SameSite=Strict`,
  `Path=/api/v1/auth`, and `Secure` outside debug mode.
- Every failure returns the same 401 and the same message, whether the account
  doesn't exist, the password is wrong, or the account is deactivated.
- When the account doesn't exist, a dummy hash is verified anyway, so "no such
  user" takes as long as "wrong password".

### The access token

Claims: `sub` (the user id), `iat`, `exp`, and `type: "access"`. It is signed
with HS256 using `FASTAPI_SECRET_KEY`. Decoding names that one algorithm
explicitly and requires all four claims.

The user is loaded from the database on every request. A deactivated account
is refused at once, not when its token expires.

### Refresh, rotation and reuse detection

Only the SHA-256 of a refresh token is stored, as the document's `_id`, so a
leaked database holds no usable tokens.

Each refresh token works once. `POST /auth/refresh` issues a new access token
and a new refresh token, and retires the old one. All tokens descended from
one login share a `family_id`: that family is the session.

If a token that was already traded in is presented again, two parties hold
tokens of the same session and the server cannot tell which is the thief. It
revokes the whole family and logs a warning to the security log.

The order of the two writes matters. The replacement is stored *first* and
the old token retired *second*, so the session always has a stored, live
token. Anything that ends the session meanwhile therefore reaches the
replacement too. The first version did it the other way round, and a refresh
in flight could survive a logout.

### Logout

- `POST /auth/logout` revokes the whole family, not just the token presented.
  If a thief has already rotated a stolen token, the copy in the user's
  browser is stale, and revoking only that would leave the thief logged in.
- `POST /auth/logout-all` revokes every session of the user.
- Access tokens already issued keep working until they expire, at most 15
  minutes later.

### The Origin check

A browser attaches cookies by itself, so another site could make a visitor's
browser call the two routes that read the cookie. `SameSite=Strict` already
stops that, and `verify_origin` is the second layer: on `/auth/refresh` and
`/auth/logout` the `Origin` header must be the API's own origin or one listed
in `FASTAPI_CORS_ALLOWED_ORIGINS`.

A request with no `Origin` header is allowed: curl, mobile apps and other
servers don't send one, and they are not cross-site browser requests.

There is no CSRF middleware because every other route authenticates with the
`Authorization` header, which a browser never attaches on its own.

### What ends sessions

| Event | Ends |
| ----- | ---- |
| `POST /auth/logout` | The session of the cookie presented |
| `POST /auth/logout-all` | Every session of the caller |
| A refresh token used twice | That session |
| Changing the password | Every session of the user, the caller's included |
| Deactivating an account, and reactivating it | Every session of that user |

"Ends" means the refresh tokens are revoked. An access token already issued
keeps working until it expires, fifteen minutes at most, except for a
deactivated account: the user is loaded on every request, so that token
stops at once.

## What a first-time reader should know

- Two tabs refreshing at the same instant look like reuse, and that session
  ends. This is the strict choice; a grace window could be added.
- A session has no absolute lifetime: each refresh grants a fresh 14 days.
- `POST /auth/register` answers 409 for a taken email, which tells a stranger
  the address has an account. Closing that needs email verification, which is
  out of scope for this version.
- Login is rate limited by address (IPv6 by network), and an account is
  locked for a while after ten failures; see [rate-limiting.md](rate-limiting.md). Argon2 also
  runs on its own small thread pool, so a burst of logins cannot stall other
  requests.
- Lifetimes come from `FASTAPI_ACCESS_TOKEN_TTL_MINUTES` and
  `FASTAPI_REFRESH_TOKEN_TTL_DAYS`.
