output "pgbench_public_ip" {
  value = aws_instance.pgbench.public_ip
}

output "rds_endpoint" {
  value = aws_db_instance.postgres.address
}

output "inventory_path" {
  value = local_file.ansible_inventory.filename
}
