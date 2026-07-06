# Multi-stage build. The builder uses the uv image to resolve + install deps into a
# venv; the runtime is a plain slim-python image that only copies that venv, so the uv
# toolchain never ships in production (smaller image, smaller attack surface). Both bases
# are pinned by digest (Dependabot's docker ecosystem bumps them) for reproducible,
# supply-chain-safe builds. The runtime base is the same python:3.12-slim-bookworm the uv
# image derives from, so the venv's interpreter symlink (/usr/local/bin/python3.12) resolves.

# ---- Builder: resolve + install deps with uv ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Copy only the manifest + lock so the dep layer caches independently of app code. A
# BuildKit cache mount keeps uv's download cache across rebuilds; --frozen uses exactly
# the pinned versions and never re-resolves. The project is `package = false`, so the app
# source isn't needed to build the venv.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- Runtime: slim python, no uv ----
FROM python:3.14-slim-bookworm@sha256:4ff4b92a68355dbdb52584ab3391dff8d371a61d4e063468bfd0130e3189c6d9 AS runtime

# Put the venv first on PATH so `python`, `daphne`, and `celery` resolve to it directly —
# no `uv run` in the runtime image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Bring in the pre-built venv, then the app code. `.venv` is dockerignored, so `COPY . .`
# never clobbers the venv copied from the builder.
COPY --from=builder /app/.venv /app/.venv
COPY . .

# Collect static into the image so WhiteNoise serves it in prod. DEBUG=false selects the
# compressed, hashed manifest backend; a throwaway secret satisfies settings import (no
# DB/Redis is touched). Running the venv's python here also self-validates the venv symlink.
RUN DJANGO_SECRET_KEY=build DJANGO_DEBUG=false python manage.py collectstatic --noinput

# Drop root: run as an unprivileged user (hardening).
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Readiness (DB + broker) — platforms use their own probes; this covers `docker run`.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/readyz').status==200 else 1)"

# Production default: daphne serves ASGI (HTTP + WebSocket) — see config/asgi.py.
# docker-compose overrides this with runserver for dev, which also serves ASGI once
# `daphne` precedes staticfiles in INSTALLED_APPS.
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
