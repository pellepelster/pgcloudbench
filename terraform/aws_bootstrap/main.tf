resource "aws_key_pair" "pgcloudbench" {
  key_name   = "pgcloudbench"
  public_key = file(pathexpand(var.ssh_public_key_path))
}
