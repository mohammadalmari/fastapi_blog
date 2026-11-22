# Security

## What is this?

The protections that apply to every request, whatever the feature: one switch
between development and production, host and HTTPS checks, response headers,
CORS, the password policy, and limits on expensive work. Who may do what is a
separate topic: see [roles-and-permissions.md](roles-and-permissions.md).

## Why one switch, and why production is the default

`FASTAPI_DEBUG` is the only setting that changes behaviour between
environments. It defaults to off.

The common failure is a production server running with development settings
because nobody set a variable. Here the opposite happens: forgetting to
configure anything gives the strict behaviour, and the app refuses to start
until the secrets it needs are supplied.

| | Debug on | Debug off (production) |
| - | -------- | ---------------------- |
| `FASTAPI_SECRET_KEY` | A built-in development key | Required, at least 32 characters, not the development key |
| `FASTAPI_ALLOWED_HOSTS` | `localhost` and `127.0.0.1` are added | Required, no `*` |
| HTTP | Served | Redirected to HTTPS, with HSTS |
| `/docs`, `/redoc`, `/openapi.json` | Served | 404 |
| CORS | Any origin | Only `FASTAPI_CORS_ALLOWED_ORIGINS`, no `*` |
| Refresh cookie | Not `Secure` | `Secure` |
| Password policy | Length only (8) | Length (12), not all digits, not like the email or username |
| Log files | None, console only | Rotating `error.log` and `security.log` |

## Where the code is

| What | Where |
| ---- | ----- |
| The values and the switch | `app/core/settings/` (`environment.py`, `security.py`) |
| Middleware that enforces them | `app/core/security/middleware.py` |
| Password hashing and policy | `app/core/security/passwords.py` |
| Error responses | `app/core/errors.py` |
| Logging | `app/core/logs.py` |

Fixed security values are constants in `app/core/settings/security.py`, not
environment variables: there is no reason for a deployment to weaken them.

## How it works

### The middleware, outermost first

1. **Request id**: gives the request its id, for the response header and
   for every log line written further in.
2. **Security headers**, added to every response, including errors and
   redirects: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
   `Referrer-Policy: same-origin`, `Cross-Origin-Opener-Policy: same-origin`,
   a Content-Security-Policy, and `Strict-Transport-Security` in production.
3. **Trusted host**: a request whose `Host` header isn't in
   `FASTAPI_ALLOWED_HOSTS` gets a 400.
4. **HTTPS redirect**, in production only.
5. **CORS**, close to the app so its headers are on error responses too.
6. **Unhandled errors**, innermost: an error nothing else handled is logged
   with its traceback and answered with a plain 500. FastAPI would do both
   itself, but from outside every layer above, so its 500 would leave without
   the security headers or the request id, and the traceback would be logged
   after the id was gone. That is the one request the id is for.

One response still bypasses these layers: the 413 for a body over the size
limit, which FastAPI sends before any middleware runs.

The Content-Security-Policy is `default-src 'none'; frame-ancestors 'none'`:
this is a JSON API and nothing it returns should load anything. The
interactive docs pages get a relaxed policy of their own, since they load
scripts and styles from a CDN.

### Behind a proxy

In production the app redirects plain HTTP. Behind a TLS-terminating proxy
every request arrives as plain HTTP, so the server must be told to trust the
proxy's `X-Forwarded-Proto` header:

```bash
uv run fastapi run --forwarded-allow-ips=<proxy ip>
```

Without it the redirect loops forever. `FORWARDED_ALLOW_IPS` also works, but
only as a real environment variable: the server reads it before the app
exists, so it is not read from `.env`.

### Passwords

Hashed with Argon2id through pwdlib's recommended settings. A hash stores its
own parameters, so if a later version raises them, old hashes still verify
and login quietly stores an upgraded one.

Hashing is slow on purpose, about 64 MiB and 30 ms each. Two consequences:

- It blocks, so async code calls `hash_password_async` and
  `verify_password_async`, which run it in a worker thread.
- Those threads come from a dedicated pool of four, not the default one
  FastAPI uses for other work. Before that change, 200 concurrent bad logins
  slowed `/health` from 3 ms to about 2 seconds.

Passwords longer than 128 characters are refused, so a huge "password"
cannot be used to burn CPU.

### Request size

A request body may be at most 1 MiB; beyond that FastAPI answers 413, in
plain text and before the route runs. A route that accepts uploads raises the
limit for itself.

### Uploads

The one upload is the avatar; [features/users.md](../features/users.md) has
the detail. The rules any later upload should follow:

- Never store what was sent. Decode it and write it again, so that nothing
  hidden in the file, and none of its metadata, survives.
- Limit three things: the request while it arrives, the file, and what the
  file unpacks into. The last is checked before unpacking.
- The app names the file. Nothing a client sent goes into a path.
- The route that serves files accepts only names of the shape the app makes,
  and every response carries `X-Content-Type-Options: nosniff`.

### Errors don't echo input

FastAPI's default answer to invalid input includes what was sent. For a
sign-up with one missing field that is the whole body, password included.
The app's validation handler drops the echoed input and keeps the location
and message.

Other errors are `{"detail": "..."}`. Services raise the classes in
`app/core/errors.py`, and one handler turns them into responses.

### The security log

Events worth an operator's attention go to the `app.security` logger: failed
logins, a locked account, refresh-token reuse, rejected origins, a wrong
current password, a missing default role. In production it is written to
`security.log`.

### Request ids and log format

Every request gets an id. It is in the response's `X-Request-ID` header and
on every log line written while that request was handled, so the lines for
one request can be found among everyone else's. An id sent by a proxy in
front is kept if it looks like one; anything else is replaced, because the
header is the client's to write and ends up in the logs.

In development a log line is a sentence. In production it is one JSON object
per line (`time`, `level`, `logger`, `message`, `request_id`, and
`exception` for an error), which a log collector can filter by field. That
includes the server's own lines: uvicorn's handlers are removed in
production and its records pass through the app's. JSON
also escapes line breaks, so text a user got into a message can't be made to
look like a second entry.

## What a first-time reader should know

- Hiding `/docs` in production is one layer among several, not a protection
  by itself.
- In debug mode, CORS allows every origin, and with
  `FASTAPI_CORS_ALLOW_CREDENTIALS=true` it does so with credentials too. That
  is only for local development against a frontend on another port.
- Guessing, expensive writes and sign-up are rate limited; reads are not.
  [rate-limiting.md](rate-limiting.md) has the table and the reasons.
- Malformed JSON gets a 422 before the login check, because FastAPI parses
  the body before resolving dependencies. Nothing is written or revealed.
- Never reuse FastAPI's own variables `FASTAPI_ENV`, `FASTAPI_PUBLIC_URL` or
  `FASTAPI_OTEL_AUTO_CONFIGURE` for app settings.
