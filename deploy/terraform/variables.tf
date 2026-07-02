variable "app_version" {
  description = "Foreman image tag to bootstrap the services with (CI re-pins on each release)."
  type        = string
  default     = "latest"
}

variable "web_subdomain" {
  description = "Subdomain for the public *.up.railway.app domain on the web service."
  type        = string
  default     = "foreman-demo"
}
