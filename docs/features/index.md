# Features

One page per folder under `app/`. Each says what the feature owns, where its
parts are, the rules it enforces, and the decisions behind it.

| Page | What it covers |
| ---- | -------------- |
| [core.md](core.md) | `app/core/`: settings, security middleware, database access, errors, pagination, logging, the health check |
| [auth.md](auth.md) | `app/auth/`: sign-up, login, refresh tokens, logout, and the current-user dependency |
| [users.md](users.md) | `app/users/`: the user model and repository, `/users/me`, role assignment |
| [posts.md](posts.md) | `app/posts/`: posts, drafts and publishing, slugs, the feed with tag and author filters, tags |
| [comments.md](comments.md) | `app/comments/`: comments and one level of replies, deletion and placeholders, the counters kept in transactions |
| [likes.md](likes.md) | `app/likes/`: likes on posts and comments, one per person, counted in transactions; `liked_by_me` |
| [rbac.md](rbac.md) | `app/rbac/` and `app/scripts/seed.py`: the permission catalog, roles, checks, startup sync, the seed command |

## The shape every feature shares

| File | Holds | May import FastAPI? |
| ---- | ----- | ------------------- |
| `router.py` | The routes: parse the request, call the service, shape the response | Yes |
| `dependencies.py` | Provider functions that build the service and repository for a request | Yes |
| `service.py` | The rules and decisions | No |
| `repository.py` | Every database query for the feature's collections | No |
| `schemas.py` | What the API accepts and returns, when it has bodies | No |
| `models.py` | The internal model, never returned as is, when it has one | No |

Services and repositories are plain classes with plain constructors, so they
can be used from a script or a test without a web server. `app/scripts/seed.py`
does exactly that.

## Adding a feature

Create `docs/features/<name>.md`, add a row above, and add its routes to
[../api.md](../api.md).
