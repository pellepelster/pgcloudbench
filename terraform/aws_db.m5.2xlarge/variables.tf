variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "instance_type" {
  type    = string
  default = "t3.2xlarge"
}

variable "db_instance_class" {
  type    = string
  default = "db.m5.2xlarge"
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
  default = ""
}
