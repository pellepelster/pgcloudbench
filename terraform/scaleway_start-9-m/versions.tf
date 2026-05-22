terraform {
  required_version = ">= 1.5"

  required_providers {
    scaleway = {
      source  = "scaleway/scaleway"
      version = "~> 2.40"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}
