module "aws_rds" {
  source = "../modules/aws_rds"
  db_instance_class = "db.m6gd.2xlarge"
  output_path = var.output_path
  iops = var.iops
}