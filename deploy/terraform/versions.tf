terraform {
  required_version = ">= 1.5"

  required_providers {
    railway = {
      source  = "terraform-community-providers/railway"
      version = "~> 0.6"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# Needs a Railway ACCOUNT/workspace token (project creation) — broader than the
# project token the CD script uses. Supply via RAILWAY_TOKEN env var.
provider "railway" {}
