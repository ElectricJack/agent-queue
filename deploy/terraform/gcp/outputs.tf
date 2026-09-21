output "vm_name" {
  description = "Compute instance name."
  value       = google_compute_instance.vm.name
}

output "ssh_command" {
  description = "SSH in over IAP. The VM has no external IP, so this is the way in."
  value       = "gcloud compute ssh ${google_compute_instance.vm.name} --zone ${var.zone} --project ${var.project_id} --tunnel-through-iap"
}

output "dashboard_tunnel_command" {
  description = <<-EOT
    Forward the dashboard to your machine, then open http://127.0.0.1:<port>.
    This is the only intended way to reach the web layer: it has effectively no
    authentication, so anything that can reach the port can run every command.
  EOT
  value       = "gcloud compute start-iap-tunnel ${google_compute_instance.vm.name} ${var.dashboard_port} --local-host-port=localhost:${var.dashboard_port} --zone ${var.zone} --project ${var.project_id}"
}

output "harness_login_commands" {
  description = <<-EOT
    Run once on the VM, after the stack is up. Each harness needs its own
    credential and each persists on its own volume. AQ never types a credential
    for you.
  EOT
  value = join("\n", [
    "cd /opt/aq/agent-queue/deploy",
    "sudo docker compose -f docker-compose.prod.yml exec daemon claude auth login",
    "sudo docker compose -f docker-compose.prod.yml exec daemon codex login --device-auth",
  ])
}

output "database_private_ip" {
  description = "Cloud SQL private IP. Reachable only from the peered VPC."
  value       = google_sql_database_instance.pg.private_ip_address
}

output "database_url_secret" {
  description = "Secret Manager secret holding the full DSN; the VM reads it at boot."
  value       = google_secret_manager_secret.database_url.secret_id
}

output "data_disk" {
  description = <<-EOT
    The disk that must outlive the instance. It carries the vault, the base
    clones and every worktree slot, and it has prevent_destroy set.
  EOT
  value       = google_compute_disk.data.name
}

output "provisioning_model" {
  description = "STANDARD or SPOT. See DESIGN.md §8 before switching."
  value       = var.provisioning_model
}
