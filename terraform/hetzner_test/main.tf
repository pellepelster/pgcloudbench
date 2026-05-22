provider "hcloud" {
}

resource "hcloud_ssh_key" "default" {
  name       = "pgcloudbench"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

resource "hcloud_server" "postgres" {
  name        = "postgres"
  server_type = var.server_type
  location    = var.location
  image       = "debian-13"
  ssh_keys    = [hcloud_ssh_key.default.id]

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }
}

resource "hcloud_server" "pgbench" {
  name        = "pgbench"
  server_type = var.server_type
  location    = var.location
  image       = "debian-13"
  ssh_keys    = [hcloud_ssh_key.default.id]
}

resource "local_file" "ansible_inventory" {
  filename        = "${var.output_path}/ansible_inventory"
  file_permission = "0644"
  content = templatefile("${path.module}/templates/inventory.tmpl", {
    postgres_host_name   = hcloud_server.postgres.name
    postgres_host_ip     = hcloud_server.postgres.ipv4_address
    pgbench_host_name    = hcloud_server.pgbench.name
    pgbench_host_ip      = hcloud_server.pgbench.ipv4_address
    ssh_private_key_path = pathexpand(var.ssh_private_key_path)
  })
}

resource "local_file" "db_endpoint" {
  filename        = "${var.output_path}/db_endpoint"
  file_permission = "0644"
  content         = hcloud_server.postgres.ipv4_address
}
