# Single-stage image; uv manages the environment. The base image is pinned by digest
# (Dependabot's docker ecosystem bumps it) for a reproducible, supply-chain-safe build.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install deps first (layer-cached) — copy the manifest + lock for a frozen,
# reproducible sync (uses exactly the pinned versions; never re-resolves).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App code.
COPY . .

# Collect static into the image so WhiteNoise serves it in prod. DEBUG=false selects the
# compressed, hashed manifest backend; a throwaway secret satisfies settings import (no
# DB/Redis is touched).
RUN DJANGO_SECRET_KEY=build DJANGO_DEBUG=false uv run --no-dev python manage.py collectstatic --noinput

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
CMD ["uv", "run", "--no-dev", "daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
