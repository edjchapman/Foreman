variable "app_version" {
  description = "Foreman image tag to bootstrap the services with (CI re-pins on each release)."
  type        = string
  default     = "latest"

  validation {
    condition     = can(regex("^(latest|\\d+\\.\\d+\\.\\d+)$", var.app_version))
    error_message = "app_version must be \"latest\" or a bare semver like \"0.7.0\" (GHCR image tags carry no \"v\")."
  }
}

variable "otel_exporter_otlp_endpoint" {
  description = "OTLP/gRPC endpoint for a trace backend (e.g. Grafana Cloud Tempo / Honeycomb). Empty leaves tracing OFF in prod. Supply via TF_VAR_otel_exporter_otlp_endpoint (a gh secret in CD)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "otel_exporter_otlp_headers" {
  description = "OTLP auth headers for the vendor, e.g. \"authorization=Bearer <token>\". Supply via TF_VAR_otel_exporter_otlp_headers (a gh secret in CD)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "otel_sampler_ratio" {
  description = "Head-sampling ratio for root spans (parent-based). 1.0 suits the low-traffic demo."
  type        = string
  default     = "1.0"
}

variable "web_subdomain" {
  description = "Subdomain for the public *.up.railway.app domain on the web service."
  type        = string
  default     = "foreman-demo"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*$", var.web_subdomain))
    error_message = "web_subdomain must be lowercase alphanumeric with hyphens (a valid DNS label)."
  }
}
