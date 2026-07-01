"""Django settings for Foreman — 12-factor style (config from the environment)."""

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-secret-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0").split(",")

INSTALLED_APPS = [
    "daphne",  # M4: ASGI server + ASGI-capable runserver; MUST precede staticfiles
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "jobs",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=os.environ.get("DATABASE_URL", "postgres://foreman:foreman@localhost:5432/foreman"),
        conn_max_age=600,
    )
}

# === Celery / Redis (M2: async worker + transactional outbox) ===
# Broker and result backend default to the same Redis; both overridable for prod.
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", REDIS_URL)
# Tests flip this to run tasks inline (no broker); see conftest._eager_celery.
CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "false").lower() == "true"
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TIMEZONE = "UTC"
# At-least-once delivery for the worker: ack a message only after the task finishes
# (acks_late) and re-queue it if the worker is killed mid-task (reject_on_worker_lost),
# so a crash can't silently drop a job. prefetch=1 keeps at most one unacked task per
# worker, so a SIGKILL strands at most one lease for the reaper to reclaim. The Redis
# visibility_timeout is the broker-level redelivery backstop; the lease reaper is the
# faster recovery path (see JOB_LEASE_SECONDS and ADR 0002).
CELERY_TASK_ACKS_LATE = os.environ.get("CELERY_TASK_ACKS_LATE", "true").lower() == "true"
CELERY_TASK_REJECT_ON_WORKER_LOST = (
    os.environ.get("CELERY_TASK_REJECT_ON_WORKER_LOST", "true").lower() == "true"
)
CELERY_WORKER_PREFETCH_MULTIPLIER = int(os.environ.get("CELERY_WORKER_PREFETCH_MULTIPLIER", "1"))
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": int(os.environ.get("CELERY_VISIBILITY_TIMEOUT", "3600")),
}
# Beat drives two pollers: the outbox relay (publish PENDING events) and the M3
# recovery scan (re-dispatch jobs whose retry backoff has elapsed).
CELERY_BEAT_SCHEDULE = {
    "dispatch-outbox": {
        "task": "jobs.dispatch_outbox",
        "schedule": float(os.environ.get("OUTBOX_POLL_SECONDS", "1.0")),
    },
    "recover-jobs": {
        "task": "jobs.recover_jobs",
        "schedule": float(os.environ.get("RECOVER_POLL_SECONDS", "5.0")),
    },
}

# === M3 reliability: retries, backoff, dead-letter, lease ===
# Worker-owned retry/lease state lives in Postgres (Job.attempts / available_at /
# leased_until / lease_token), not the broker — queryable and broker-restart-safe.
JOB_MAX_ATTEMPTS = int(os.environ.get("JOB_MAX_ATTEMPTS", "3"))
JOB_RETRY_BASE_SECONDS = float(os.environ.get("JOB_RETRY_BASE_SECONDS", "2"))
JOB_RETRY_MAX_SECONDS = float(os.environ.get("JOB_RETRY_MAX_SECONDS", "300"))
JOB_LEASE_SECONDS = float(os.environ.get("JOB_LEASE_SECONDS", "120"))
JOB_REQUEUE_VISIBILITY_SECONDS = float(os.environ.get("JOB_REQUEUE_VISIBILITY_SECONDS", "60"))

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
}

# === Observability: structured logging (M4) ===
# One JSON object per log line (see config.logformat.JsonFormatter), toggled to a
# human-readable format for local dev via DJANGO_LOG_FORMAT=console. See ADR 0003.
LOG_FORMAT = os.environ.get("DJANGO_LOG_FORMAT", "json")  # "json" (default) | "console"
# Celery hijacks the root logger by default and would reformat worker logs, bypassing
# our schema in the very process that emits most job events — let them flow through
# this LOGGING config instead.
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "config.logformat.JsonFormatter"},
        "console": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": LOG_FORMAT},
    },
    # Handler lives only on root; app loggers set a level and propagate up to it, so a
    # record is emitted exactly once. Don't add a handler here to `jobs`/`celery` too —
    # that double-logs. propagate (default True) is also what lets pytest caplog capture.
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "jobs": {"level": "INFO"},
        "celery": {"level": "INFO"},
    },
}

# === Realtime: Django Channels + WebSockets (M4) ===
# Group-per-job fan-out of live status (jobs/realtime.py → jobs/consumers.py). Redis in
# prod, reusing REDIS_URL; the test suite swaps this for InMemoryChannelLayer (conftest).
# The ProtocolTypeRouter in config/asgi.py serves HTTP + WebSocket. See ADR 0004.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [os.environ.get("CHANNELS_REDIS_URL", REDIS_URL)]},
    }
}

AUTH_PASSWORD_VALIDATORS: list = []

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
