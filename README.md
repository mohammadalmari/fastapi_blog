# FastAPI Blog

A multi-user blog API built with FastAPI, featuring authentication and
authorization with dynamic, admin-managed roles and permissions.

## Tooling

- [uv](https://docs.astral.sh/uv/) — dependency and environment management
- [ruff](https://docs.astral.sh/ruff/) — linting and formatting
- [ty](https://docs.astral.sh/ty/) — type checking

## Getting started

Either way the API runs at http://127.0.0.1:8000 and interactive docs at
http://127.0.0.1:8000/docs.

### Everything in Docker

```bash
docker compose up -d --wait
```

This builds the development image and starts the API and MongoDB. The project
folder is mounted into the container, so the server reloads when you edit a
file. Useful commands:

```bash
docker compose logs -f api          # follow the server log
docker compose exec api pytest      # run the tests in the container
docker compose up -d --build        # rebuild after changing dependencies
docker compose down                 # stop; add -v to also delete the data
```

### API on your machine

```bash
uv sync
cp .env.example .env
uv run fastapi dev
```

Use one way at a time: both serve on port 8000.

The API needs a MongoDB to talk to, and `DATABASE_URL` in `.env` says where
it is. Any MongoDB works, local or hosted (MongoDB Atlas, for example), as
long as it is a replica set: the app uses transactions, which a standalone
server doesn't have. Hosted clusters are replica sets already.

For a local one, this starts only the database from the Compose file, as a
single-node replica set on `localhost:27017`, which is what `.env.example`
points at:

```bash
docker compose up -d --wait mongo
```

The app pings the database at startup and refuses to start if it can't reach
it.

## Accounts and login

All routes are under `/api/v1`; `/docs` lists them and its "Authorize" button
logs you in.

| Route | What it does |
|---|---|
| `POST /auth/register` | Create an account (JSON: `email`, `username`, `password`) |
| `POST /auth/login` | Log in with an email or username (form: `username`, `password`) |
| `POST /auth/refresh` | Get a new access token using the refresh cookie |
| `POST /auth/logout` | End this session |
| `POST /auth/logout-all` | End every session of the logged-in user |
| `GET /users/me` | The logged-in user |

Login returns a short-lived access token (15 minutes) to send as
`Authorization: Bearer <token>`, and sets a long-lived refresh token (14 days)
in an HttpOnly cookie that only the `/auth` routes receive. Each refresh
replaces the refresh token; presenting an already-used one ends that session.
Logging out ends the whole session, and errors come back as `{"detail": ...}`.

## Configuration

Settings are read from environment variables, falling back to `.env`; see
`.env.example` for every variable. `FASTAPI_DEBUG` is the one switch between
development and production behaviour, and production is the default: without
`FASTAPI_DEBUG=true` the app refuses to start unless `FASTAPI_SECRET_KEY` and
`FASTAPI_ALLOWED_HOSTS` are set, serves no interactive docs, redirects HTTP to
HTTPS, and only allows the CORS origins you list.

### Running in production

```bash
uv run fastapi run --forwarded-allow-ips=<proxy ip>
```

Behind a TLS-terminating proxy the server must trust the proxy's
`X-Forwarded-Proto` header, otherwise the HTTPS redirect loops forever.
`--forwarded-allow-ips` (or a real `FORWARDED_ALLOW_IPS` environment variable;
it is not read from `.env`) names the proxy to trust.

The same thing as a container image:

```bash
docker build --target production -t fastapi-blog .
```

The image holds only the runtime dependencies and the `app/` package, runs as
a non-root user, and contains no `.env`: pass `FASTAPI_SECRET_KEY`,
`FASTAPI_ALLOWED_HOSTS`, `DATABASE_URL` and `FORWARDED_ALLOW_IPS` as
environment variables. It expects a TLS-terminating proxy in front of it,
which this repository doesn't provide.

## Development

```bash
uv run ruff check .        # lint
uv run ruff format .       # format
uv run ty check            # type check
uv run pytest              # tests
```

The tests need a reachable MongoDB replica set. They connect to
`TEST_DATABASE_URL` if it is set, otherwise to `DATABASE_URL`, otherwise to
the Compose database on `localhost:27017`; both variables can be in `.env` or
the shell. On that server they use a database with a random name and drop it
afterwards, so they never touch your development data and can safely share
its server. The user in the URL must be allowed to create and drop databases.
