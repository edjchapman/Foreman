# Foreman on Railway — everything the community provider CAN express.
#
# Three settings the provider CANNOT express (provider v0.6.x omits them; and
# Railway's railway.json config-as-code doesn't apply to image-sourced
# services) are applied by scripts/railway-configure.sh (`make configure`)
# after `terraform apply` — see outputs.manual_steps and docs/deploy.md:
#   1. web:    pre-deploy command (migrate) + healthcheck path (/readyz)
#   2. worker: custom start command (celery worker)
#   3. beat:   custom start command (celery beat)

locals {
  app_image = "ghcr.io/edjchapman/foreman:${var.app_version}"

  postgres_user = "foreman"
  postgres_db   = "foreman"
  database_url  = "postgresql://${local.postgres_user}:${random_password.postgres.result}@postgres.railway.internal:5432/${local.postgres_db}"
  redis_url     = "redis://default:${random_password.redis.result}@redis.railway.internal:6379/0"

  app_env = [
    { name = "DATABASE_URL", value = local.database_url },
    { name = "REDIS_URL", value = local.redis_url },
    { name = "DJANGO_SECRET_KEY", value = random_password.django_secret.result },
    { name = "DJANGO_DEBUG", value = "false" },
  ]

  web_env = concat(local.app_env, [
    # Railway healthchecks probe the port in PORT and send
    # Host: healthcheck.railway.app — both must be declared or the deploy
    # gate can never pass (daphne listens on the image's fixed 8000).
    { name = "PORT", value = "8000" },
    # Also gates the WebSocket Origin check (AllowedHostsOriginValidator).
    { name = "DJANGO_ALLOWED_HOSTS", value = "${railway_service_domain.web.domain},healthcheck.railway.app" },
    { name = "DJANGO_CSRF_TRUSTED_ORIGINS", value = "https://${railway_service_domain.web.domain}" },
    { name = "DJANGO_SECURE_SSL_REDIRECT", value = "true" },
    { name = "DJANGO_SECURE_COOKIES", value = "true" },
    { name = "DJANGO_SECURE_HSTS_SECONDS", value = "31536000" },
  ])
}

resource "random_password" "django_secret" {
  length  = 64
  special = false # check --deploy needs length + variety, not punctuation
}

resource "random_password" "postgres" {
  length  = 32
  special = false # stays URL-safe inside DATABASE_URL
}

resource "random_password" "redis" {
  length  = 32
  special = false
}

resource "railway_project" "foreman" {
  name        = "foreman"
  description = "Event-driven job-processing platform (portfolio demo)"
}

# --- Databases -------------------------------------------------------------
# Railway's own DB "templates" are just these images + a volume; declaring
# them as plain services keeps them in state so destroy/apply is a clean
# off/on switch. Service name == private-network hostname (<name>.railway.internal).

resource "railway_service" "postgres" {
  name         = "postgres"
  project_id   = railway_project.foreman.id
  source_image = "ghcr.io/railwayapp-templates/postgres-ssl:16"

  volume = {
    name       = "pgdata"
    mount_path = "/var/lib/postgresql/data"
  }
}

resource "railway_variable_collection" "postgres" {
  service_id     = railway_service.postgres.id
  environment_id = railway_project.foreman.default_environment.id
  variables = [
    { name = "POSTGRES_USER", value = local.postgres_user },
    { name = "POSTGRES_DB", value = local.postgres_db },
    { name = "POSTGRES_PASSWORD", value = random_password.postgres.result },
    # Subdirectory of the mount — initdb refuses a non-empty root (lost+found).
    { name = "PGDATA", value = "/var/lib/postgresql/data/pgdata" },
  ]
}

# No volume: Redis holds only ephemeral state here (Celery broker/results,
# Channels layer) — durable data lives in Postgres. Also load-bearing: the
# provider (v0.6.2) creates but never attaches a second project volume, so a
# redis volume deterministically fails every fresh `apply` (the off/on switch).
resource "railway_service" "redis" {
  name       = "redis"
  project_id = railway_project.foreman.id
  # bitnamilegacy: Bitnami's 2025 catalog change removed versioned tags from
  # bitnami/* (only :latest remains); pinned tags live in the frozen legacy
  # namespace. Acceptable here: private-network-only, password-protected,
  # ephemeral broker state.
  source_image = "bitnamilegacy/redis:7.2"
}

resource "railway_variable_collection" "redis" {
  service_id     = railway_service.redis.id
  environment_id = railway_project.foreman.default_environment.id
  variables = [
    { name = "REDIS_PASSWORD", value = random_password.redis.result },
  ]
}

# --- App services (one image, three processes) ------------------------------

resource "railway_service" "web" {
  name         = "web"
  project_id   = railway_project.foreman.id
  source_image = local.app_image
}

resource "railway_service_domain" "web" {
  subdomain      = var.web_subdomain
  service_id     = railway_service.web.id
  environment_id = railway_project.foreman.default_environment.id
}

resource "railway_variable_collection" "web" {
  service_id     = railway_service.web.id
  environment_id = railway_project.foreman.default_environment.id
  variables      = local.web_env
}

resource "railway_service" "worker" {
  name         = "worker"
  project_id   = railway_project.foreman.id
  source_image = local.app_image
}

resource "railway_variable_collection" "worker" {
  service_id     = railway_service.worker.id
  environment_id = railway_project.foreman.default_environment.id
  variables      = local.app_env
}

resource "railway_service" "beat" {
  name         = "beat"
  project_id   = railway_project.foreman.id
  source_image = local.app_image
}

resource "railway_variable_collection" "beat" {
  service_id     = railway_service.beat.id
  environment_id = railway_project.foreman.default_environment.id
  variables      = local.app_env
}
