output "public_url" {
  description = "The live demo URL."
  value       = "https://${railway_service_domain.web.domain}"
}

output "github_ci_variables" {
  description = "Set these as GitHub repo variables so the release workflow can deploy."
  value = {
    RAILWAY_ENVIRONMENT_ID    = railway_project.foreman.default_environment.id
    RAILWAY_WEB_SERVICE_ID    = railway_service.web.id
    RAILWAY_WORKER_SERVICE_ID = railway_service.worker.id
    RAILWAY_BEAT_SERVICE_ID   = railway_service.beat.id
  }
}

output "manual_steps" {
  description = "Dashboard settings the provider cannot express — apply once after the first apply."
  value       = <<-EOT
    1. web    → Settings → Deploy → Pre-Deploy Command: uv run --no-dev python manage.py migrate
                Settings → Deploy → Healthcheck Path:   /readyz
    2. worker → Settings → Deploy → Custom Start Command: uv run --no-dev celery -A config worker -l info --concurrency 2
    3. beat   → Settings → Deploy → Custom Start Command: uv run --no-dev celery -A config beat -l info
    4. Project Settings → Tokens → create a production project token → gh secret set RAILWAY_TOKEN
  EOT
}
