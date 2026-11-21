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
uv run fastapi dev
```

The API runs at http://127.0.0.1:8000 and interactive docs at http://127.0.0.1:8000/docs.

## Development

```bash
uv run ruff check .        # lint
uv run ruff format .       # format
uv run ty check            # type check
```
