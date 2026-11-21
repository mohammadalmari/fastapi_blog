# Auth

`app/auth/`: signing up, logging in, and the sessions that follow. The design
is explained in [../concepts/authentication.md](../concepts/authentication.md);
this page is about the code.

## What it owns

- The routes under `/api/v1/auth`.
- The `refresh_tokens` collection.
- `get_current_user` and `CurrentUserDep`, which every protected route in the
  app depends on, directly or through the principal.
- `get_optional_user` and `OptionalUserDep`, for public routes that show more
  to a logged-in user: None without a token, and still a 401 for a bad one.

It does not own users. It creates and reads them through `UserRepository`.

## The collection

`refresh_tokens`, one document per token:

| Field | Notes |
| ----- | ----- |
| `_id` | SHA-256 of the token. The token itself is never stored |
| `user_id` | Indexed |
| `family_id` | Indexed. Shared by every token descended from one login |
| `expires_at` | TTL index: MongoDB deletes the document once this has passed |
| `revoked_at` | Null while the token is live |
| `replaced_by` | The hash of the token that replaced it, when it was rotated |
| `created_at`, `user_agent` | For an eventual "your sessions" screen; the user agent is cut to 256 characters |

## The service

`AuthService` in `app/auth/service.py`:

| Method | What it does | Refuses with |
| ------ | ------------ | ------------ |
| `register` | Checks the password policy, hashes, creates the user with the default role | 422 for the password, 409 for a taken email or username |
| `login` | Verifies the password, upgrades an outdated hash, starts a new token family | 401, one message for every cause |
| `refresh` | Stores a replacement, retires the old token, detects reuse | 401 |
| `logout` | Revokes the token's family | Nothing; safe to call twice |
| `logout_all` | Revokes every token of the user | Nothing |

## Rules that are easy to break

- **Store the replacement before retiring the old token.** The other order
  leaves a moment in which the session has no live token, and a revocation in
  that moment misses the replacement.
- **A revoked token with `replaced_by` set is reuse; without it, a logout.**
  Only the first is logged as a security event.
- **Always verify a hash at login**, the dummy one when the account doesn't
  exist.
- **Revoke by family or by user, never a single token**, when ending a
  session.

## Key decisions

- **The token's hash is the document id.** Lookup is by primary key and no
  second index is needed.
- **Rotation is not a transaction.** One atomic update, "retire this token if
  it is still live", decides the winner when two requests race, and the loser
  revokes the family.
- **The role repository is injected through `rbac.providers`.** The usual
  place, `rbac.dependencies`, imports this feature's dependencies, and that
  would be a circular import.

## Tests

`tests/auth/test_auth.py` over HTTP, `tests/auth/test_auth_production.py` for what only
exists in production mode (the `Secure` cookie, the strict password policy,
the Origin check), and unit tests in `tests/core/test_tokens.py` and
`tests/core/test_passwords.py`.
