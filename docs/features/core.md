# Core

`app/core/` holds what every feature shares. Nothing in it imports from a
feature.

## What is in it

| Module | What it provides |
| ------ | ---------------- |
| `settings/` | One `Settings` class built from three modules: `environment` (the debug switch, secret key, allowed hosts), `security` (constants and security settings), `services` (database, logging) |
| `security/middleware.py` | `install_security(app, settings)`: trusted host, HTTPS redirect, CORS, security headers |
| `security/passwords.py` | Argon2 hashing, the async wrappers on their own thread pool, the password policy |
| `security/tokens.py` | Access tokens (JWT) and refresh tokens (random strings and their hashes) |
| `database.py` | The client, `DbDep`, `register_indexes`, `in_transaction`, `utcnow` |
| `errors.py` | The errors services raise, and the handlers that turn them into responses |
| `types.py` | `PyObjectId` |
| `pagination.py` | `PageParamsDep`, `Page[T]`, and the cursor helpers |
| `logs.py` | `configure_logging(settings)` and the `app.security` logger name |
| `health.py` | `GET /health` |

## Settings

- Routes and dependencies take `SettingsDep`, which hands out the settings
  the running app was built with (kept on `app.state.settings`).
- `get_settings()` reads the environment, once per process. It is for code
  that runs outside a request, such as the seed command.
- Variables use the `FASTAPI_` prefix, except `DATABASE_URL` and
  `DATABASE_NAME`. `.env.example` lists them all.
- The three settings modules import only from `environment`, never from each
  other.

Why two ways to get settings: the app is built by `create_app(settings)`, and
tests pass their own. A route that called `get_settings()` would read the
environment and ignore what the app was built with.

## Errors

| Class | Status | For |
| ----- | ------ | --- |
| `UnauthorizedError` | 401 | No credentials, or ones not accepted |
| `ForbiddenError` | 403 | Not allowed to do this |
| `NotFoundError` | 404 | It doesn't exist |
| `ConflictError` | 409 | Clashes with what exists |
| `FieldError` | 422 | A field or query parameter failed a check Pydantic couldn't make, because it depends on settings or the database |

Services raise these, never FastAPI's `HTTPException`, so the business logic
doesn't depend on the web framework. `FieldError` is rendered in the same
shape as FastAPI's own validation errors, so clients handle one format.

## Key decisions

- **An app factory.** `create_app()` builds the app, and `app.main:app` is one
  built from the environment. Middleware is fixed when an app is created, so
  tests need the factory to build a production-mode one. The official docs
  create `app` at module level; this is a deliberate difference.
- **A plain ASGI class for the headers middleware**, not
  `@app.middleware("http")`. The decorator is built on Starlette's
  `BaseHTTPMiddleware`, which runs the rest of the app in a separate task and
  has rough edges with streaming responses. The class only edits the message
  that starts a response, and it is outermost, so its headers are also on
  responses that other middleware produces.
- **`async def` dependencies even when they await nothing.** FastAPI runs a
  plain `def` dependency in its thread pool, which is a wasted hop for a
  lookup and competes with real blocking work.
- **`/health` pings the database.** A process that is up but cannot reach its
  database is not healthy.

See also [../concepts/security.md](../concepts/security.md) and
[../concepts/database.md](../concepts/database.md).
