# syntax=docker/dockerfile:1

# Two images from one file:
#   development  dev dependencies, auto-reload, code bind-mounted by Compose
#   production   runtime dependencies only, code baked in, non-root user
#
#   docker build --target production -t fastapi-blog .

# ---- Base: shared by every stage ----
FROM python:3.14-slim-bookworm AS base

# The virtualenv lives outside the app directory, so that bind-mounting the
# project over /usr/src/app in development doesn't hide it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/virtualenvs/.venv \
    PATH="/virtualenvs/.venv/bin:$PATH"

WORKDIR /usr/src/app

# ---- uv: installs dependencies, never part of the production image ----
FROM base AS uv

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /bin/uv

# Install into $VIRTUAL_ENV with the image's Python, and copy files instead of
# hard-linking them from the build cache, which is on another filesystem.
ENV UV_PROJECT_ENVIRONMENT=/virtualenvs/.venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

# Only the dependency files: this layer is rebuilt when they change, not on
# every code edit. --locked fails the build if uv.lock is out of date.
COPY pyproject.toml uv.lock ./

# ---- Builder: runtime dependencies only ----
FROM uv AS builder

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --compile-bytecode

# ---- Development ----
FROM uv AS development

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

# The code itself comes from the bind mount in docker-compose.yml.
EXPOSE 8000

CMD ["fastapi", "dev", "--host", "0.0.0.0", "--port", "8000"]

# ---- Production ----
FROM base AS production

RUN adduser --disabled-password --gecos "" fastapi

COPY --from=builder ${VIRTUAL_ENV} ${VIRTUAL_ENV}

# pyproject.toml names the app for the `fastapi` command ([tool.fastapi]).
COPY --chown=fastapi:fastapi pyproject.toml ./
COPY --chown=fastapi:fastapi app ./app

# In production the app writes rotating log files to ./logs and uploaded
# avatars to ./media. Mount a volume over media, or they are lost with the
# container.
RUN mkdir -p logs media && chown fastapi:fastapi logs media

USER fastapi

EXPOSE 8000

# Configuration comes from real environment variables; no .env is copied in.
# Behind a TLS-terminating proxy also set FORWARDED_ALLOW_IPS to the proxy's
# address, or the HTTPS redirect loops (see README).
CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8000"]
