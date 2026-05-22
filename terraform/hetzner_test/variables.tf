variable "server_type" {
  type        = string
  default     = "cx23"
}

variable "location" {
  type        = string
  default     = "nbg1"
}

variable "ssh_public_key_path" {
  type        = string
  description = "Path to the SSH public key uploaded to root on the server."
  default     = "~/.ssh/id_ed25519.pub"
}

variable "ssh_private_key_path" {
  type        = string
  description = "Path to the matching SSH private key Ansible will use."
  default     = "~/.ssh/id_ed25519"
}

variable "output_path" {
  type        = string
  description = "Absolute path to the output directory for this testbed."
  default     = ""
}
