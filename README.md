# FastAPI Blog

A multi-user blog API built with FastAPI, featuring authentication and
authorization with dynamic, admin-managed roles and permissions.

## Tooling

- [uv](https://docs.astral.sh/uv/) — dependency and environment management
- [ruff](https://docs.astral.sh/ruff/) — linting and formatting
- [ty](https://docs.astral.sh/ty/) — type checking

## Getting started

```bash
uv sync
cp .env.example .env
docker compose up -d --wait
uv run fastapi dev
```

The API runs at http://127.0.0.1:8000 and interactive docs at http://127.0.0.1:8000/docs.

`docker compose up` starts MongoDB as a single-node replica set on
`localhost:27017`, which the app needs for transactions. The app pings the
database at startup and refuses to start if it can't reach it. Stop MongoDB
with `docker compose down`; add `-v` to also delete its data.

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

## Development

```bash
uv run ruff check .        # lint
uv run ruff format .       # format
uv run ty check            # type check
uv run pytest              # tests
```

The tests need MongoDB running (`docker compose up -d --wait`). They use a
database with a random name and drop it afterwards, so they never touch your
development data.
