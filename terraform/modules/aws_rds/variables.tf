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
}

variable "ssh_private_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519"
}

variable "output_path" {
  type    = string
}

variable "iops" {
  type = number
}
