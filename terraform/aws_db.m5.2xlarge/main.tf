provider "aws" {
  region = var.region
}

data "aws_ami" "debian13" {
  most_recent = true
  owners      = ["136693071363"]

  filter {
    name   = "name"
    values = ["debian-13-amd64-*"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_key_pair" "default" {
  key_name   = "pgcloudbench"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

resource "aws_security_group" "ec2" {
  name        = "pgcloudbench-ec2"
  description = "pgbench EC2 instance"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "rds" {
  name        = "pgcloudbench-rds"
  description = "RDS PostgreSQL accessible from pgbench EC2 only"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "PostgreSQL from pgbench EC2"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ec2.id]
  }
}

resource "aws_db_subnet_group" "default" {
  name       = "pgcloudbench"
  subnet_ids = data.aws_subnets.default.ids
}

resource "random_password" "rds" {
  length  = 32
  special = false
}

resource "aws_db_instance" "postgres" {
  identifier        = "pgcloudbench"
  engine            = "postgres"
  engine_version    = "18"
  instance_class    = var.db_instance_class
  allocated_storage = 1000
  storage_type      = "gp2"

  db_name  = "pgbench"
  username = "postgres"
  password = random_password.rds.result

  db_subnet_group_name   = aws_db_subnet_group.default.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  publicly_accessible       = false
  multi_az                  = false
  backup_retention_period   = 0
  skip_final_snapshot       = true
  deletion_protection       = false
}

resource "aws_instance" "pgbench" {
  ami                         = data.aws_ami.debian13.id
  instance_type               = var.instance_type
  key_name                    = aws_key_pair.default.key_name
  vpc_security_group_ids      = [aws_security_group.ec2.id]
  associate_public_ip_address = true

  tags = {
    Name = "pgcloudbench-pgbench"
  }
}

resource "local_file" "ansible_inventory" {
  filename        = "${var.output_path}/ansible_inventory"
  file_permission = "0644"
  content = templatefile("${path.module}/templates/inventory.tmpl", {
    pgbench_host_name    = aws_instance.pgbench.tags["Name"]
    pgbench_host_ip      = aws_instance.pgbench.public_ip
    ssh_private_key_path = pathexpand(var.ssh_private_key_path)
  })
}

resource "local_file" "db_endpoint" {
  filename        = "${var.output_path}/db_endpoint"
  file_permission = "0644"
  content         = aws_db_instance.postgres.address
}

resource "local_file" "db_password" {
  filename        = "${var.output_path}/db_password"
  file_permission = "0600"
  content         = random_password.rds.result
}
