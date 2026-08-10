output "public_url" {
  description = "The live demo URL."
  value       = var.custom_domain != "" ? "https://${var.custom_domain}" : "https://${railway_service_domain.web.domain}"
}

output "custom_domain_dns" {
  description = "DNS records to create for the custom domain (CNAME host_label -> dns_record_value; verification record only if Railway asks for one)."
  value = var.custom_domain == "" ? null : {
    host_label                = railway_custom_domain.web[0].host_label
    dns_record_value          = railway_custom_domain.web[0].dns_record_value
    verification_host_label   = railway_custom_domain.web[0].verification_host_label
    verification_record_value = railway_custom_domain.web[0].verification_record_value
  }
}

output "github_ci_variables" {
  description = "Set these as GitHub repo variables so the release workflow can deploy."
  value = {
    RAILWAY_ENVIRONMENT_ID      = railway_project.foreman.default_environment.id
    RAILWAY_WEB_SERVICE_ID      = railway_service.web.id
    RAILWAY_WORKER_SERVICE_ID   = railway_service.worker.id
    RAILWAY_BEAT_SERVICE_ID     = railway_service.beat.id
    RAILWAY_LISTENER_SERVICE_ID = railway_service.listener.id
  }
}

output "manual_steps" {
  description = "One-time steps after apply — everything else the provider can't express is scripted."
  value       = <<-EOT
    1. Project Settings → Tokens → create a production project token → gh secret set RAILWAY_TOKEN
    2. RAILWAY_TOKEN=<that token> make configure
       (deploy/scripts/railway-configure.sh — sets the deploy settings the provider
        can't express: web pre-deploy migrate + /readyz healthcheck,
        worker/beat celery start commands, listener push-dispatch start command)
    3. Set the GitHub repo variable RAILWAY_LISTENER_SERVICE_ID (from
       `terraform output -json github_ci_variables`) so the release workflow
        deploys the listener too — until then CD skips it and Beat's poll dispatches.
  EOT
}
