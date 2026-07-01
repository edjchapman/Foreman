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

EXPOSE 8000

# Production default: daphne serves ASGI (HTTP + WebSocket) — see config/asgi.py.
# docker-compose overrides this with runserver for dev, which also serves ASGI once
# `daphne` precedes staticfiles in INSTALLED_APPS.
CMD ["uv", "run", "--no-dev", "daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
