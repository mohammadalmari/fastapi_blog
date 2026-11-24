# Draftwork Blog

A multi-user blog API built with FastAPI, with authentication and
authorization through roles and permissions that administrators manage while
the app runs.

- Live site: https://draftwork-blog.netlify.app/
- Web frontend: [fastapi_blog-web](https://github.com/mohammadalmari/fastapi_blog-web)

Built with [uv](https://docs.astral.sh/uv/) (dependencies),
[ruff](https://docs.astral.sh/ruff/) (lint and format),
[ty](https://docs.astral.sh/ty/) (types), pytest, and MongoDB through
PyMongo's async client.

## Documentation

| | |
| - | - |
| [API overview](docs/api.md) | Every route and what it needs |
| [Concepts](docs/concepts/index.md) | [Authentication](docs/concepts/authentication.md), [roles and permissions](docs/concepts/roles-and-permissions.md), [security](docs/concepts/security.md), [rate limiting](docs/concepts/rate-limiting.md), [the database](docs/concepts/database.md), [testing](docs/concepts/testing.md) |
| [Features](docs/features/index.md) | One page per folder under `app/` |

With the app running in debug mode, the full API reference is at
http://127.0.0.1:8000/docs. `uv run python -m app.scripts.export_openapi`
writes its schema to a file for generating clients; see the
[API overview](docs/api.md).

## Getting started

Either way the API runs at http://127.0.0.1:8000.

### Everything in Docker

```bash
docker compose up -d --wait
```

This builds the development image and starts the API and MongoDB. The project
folder is mounted into the container, so the server reloads when you edit a
file.

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

The API needs a MongoDB, and `DATABASE_URL` in `.env` says where it is. Any
MongoDB works, local or hosted, as long as it is a replica set: the app uses
transactions, which a standalone server doesn't have. For a local one, this
starts only the database from the Compose file:

```bash
docker compose up -d --wait mongo
```

### The first administrator

New accounts get the `reader` role. To create an admin, and the optional
`author`, `moderator` and `editor` roles:

```bash
uv run python -m app.scripts.seed --email you@example.com --username you
```

It asks for a password. In Docker, put `docker compose exec api` in front and
drop `uv run`. To make an account that already exists an admin, use
`--email you@example.com --promote`.

## Configuration

Settings come from environment variables, falling back to `.env`;
`.env.example` lists every variable.

`FASTAPI_DEBUG` is the one switch between development and production, and
production is the default. Without `FASTAPI_DEBUG=true` the app refuses to
start unless `FASTAPI_SECRET_KEY` and `FASTAPI_ALLOWED_HOSTS` are set, serves
no interactive docs, redirects HTTP to HTTPS, and allows only the CORS origins
you list. [Security](docs/concepts/security.md) has the full table.

### Running in production

```bash
uv run fastapi run --forwarded-allow-ips=<proxy ip>
```

Behind a TLS-terminating proxy the server must trust the proxy's forwarded
headers, for two reasons: without `X-Forwarded-Proto` the HTTPS redirect
loops forever, and without `X-Forwarded-For` every visitor shares one
[rate limit](docs/concepts/rate-limiting.md), because they all appear to come
from the proxy. Name only the proxy's address.

In production the logs are one JSON object per line, each with the id of the
request it belongs to. The same id is in every response's `X-Request-ID`.

As a container image:

```bash
docker build --target production -t fastapi-blog .
```

Uploaded avatars are written to `./media` (`FASTAPI_MEDIA_DIR`): mount a
volume there, or they are lost with the container.

The image holds only the runtime dependencies and the `app/` package, runs as
a non-root user, and contains no `.env`: pass `FASTAPI_SECRET_KEY`,
`FASTAPI_ALLOWED_HOSTS`, `DATABASE_URL` and `FORWARDED_ALLOW_IPS` as
environment variables. It expects a TLS-terminating proxy in front of it,
which this repository doesn't provide.

## Development

```bash
uv run ruff format .       # format
uv run ruff check .        # lint
uv run ty check            # type check
uv run pytest              # tests
```

The tests need a reachable MongoDB replica set: `TEST_DATABASE_URL` if set,
otherwise `DATABASE_URL`, otherwise the Compose database. They use a database
with a random name and drop it afterwards, so they never touch your
development data. [Testing](docs/concepts/testing.md) covers the rest,
including running in parallel and measuring coverage.

To have the first three run by themselves before each commit, install the
git hook once per checkout:

```bash
uv run pre-commit install
```

The formatter rewrites what needs it and stops the commit so you can look and
stage the result; the linter and type checker only report. The tests are not
in the hook: they take longer and need MongoDB.

The same four checks run on GitHub for every push to `main` and every pull
request ([.github/workflows/ci.yml](.github/workflows/ci.yml)), against a
MongoDB replica set started for the run. With `--check` on the formatter and
`--locked` on the install, so unformatted code or a stale `uv.lock` fails.
