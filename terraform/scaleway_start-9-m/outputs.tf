output "pgbench_public_ip" {
  value = scaleway_instance_ip.pgbench.address
}

output "inventory_path" {
  value = local_file.ansible_inventory.filename
}
