variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "ssh_public_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519.pub"
}
