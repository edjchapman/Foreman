# Single-stage image; uv manages the environment.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

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

# Production default; docker-compose overrides this with runserver for dev.
CMD ["uv", "run", "--no-dev", "gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
