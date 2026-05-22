variable "zone" {
  type    = string
  default = "fr-par-2"
}

variable "instance_type" {
  type    = string
  default = "STANDARD3-X8C-32G"
}

variable "db_address" {
  type    = string
  default = ""
}

variable "ssh_public_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519.pub"
}

variable "ssh_private_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519"
}

variable "output_path" {
  type    = string
}
