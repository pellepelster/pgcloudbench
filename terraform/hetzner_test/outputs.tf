output "server_ipv4" {
  description = "Public IPv4 address of the PostgreSQL server."
  value       = hcloud_server.postgres.ipv4_address
}

output "server_ipv6" {
  description = "Public IPv6 address of the PostgreSQL server."
  value       = hcloud_server.postgres.ipv6_address
}

output "pgbench_ipv4" {
  description = "Public IPv4 address of the pgbench client server."
  value       = hcloud_server.pgbench.ipv4_address
}

output "pgbench_ipv6" {
  description = "Public IPv6 address of the pgbench client server."
  value       = hcloud_server.pgbench.ipv6_address
}

output "inventory_path" {
  description = "Path to the generated Ansible inventory file."
  value       = local_file.ansible_inventory.filename
}
