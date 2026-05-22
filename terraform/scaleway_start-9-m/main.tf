provider "scaleway" {
  zone = var.zone
}

data "scaleway_instance_image" "debian" {
  name         = "Debian Bookworm"
  architecture = "x86_64"
  latest       = true
}

resource "scaleway_iam_ssh_key" "default" {
  name       = "pgcloudbench"
  public_key = trimspace(file(pathexpand(var.ssh_public_key_path)))
}

resource "scaleway_instance_ip" "pgbench" {
  zone = var.zone
}

resource "scaleway_instance_server" "pgbench" {
  name  = "pgcloudbench-pgbench"
  type  = var.instance_type
  image = data.scaleway_instance_image.debian.id
  ip_id = scaleway_instance_ip.pgbench.id
  zone  = var.zone
  tags  = ["pgbench"]

  depends_on = [scaleway_iam_ssh_key.default]
}

resource "local_file" "ansible_inventory" {
  filename        = "${var.output_path}/ansible_inventory"
  file_permission = "0644"
  content = templatefile("${path.module}/templates/inventory.tmpl", {
    pgbench_host_name    = scaleway_instance_server.pgbench.name
    pgbench_host_ip      = scaleway_instance_ip.pgbench.address
    ssh_private_key_path = pathexpand(var.ssh_private_key_path)
    db_address           = var.db_address
  })
}
