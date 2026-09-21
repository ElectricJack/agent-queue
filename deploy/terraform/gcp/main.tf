# Agent Queue on one GCP VM — DESIGN.md §8, step 1.
#
# Shape: a single ordinary VM running the compose stack from deploy/, a
# persistent data disk that outlives it, and Cloud SQL on a private IP.
#
# The security model is the network, not the application. The web layer has
# effectively no authentication in this configuration — `api_auth` is documented
# upstream as auth for a *local* API, the dashboard sends no Authorization
# header, and a request with no header resolves to a scope that can run every
# command. So: no external IP, no ingress except IAP, and the dashboard bound to
# localhost on the VM and reached through a tunnel. Do not open this up without
# reading deploy/README.md's security posture section first.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  name = var.name
  # The VM reads these at boot; see cloud-init.yaml.
  db_name = "agent_queue"
  db_user = "agent_queue"
}

# --- APIs --------------------------------------------------------------------

resource "google_project_service" "required" {
  for_each = toset([
    "compute.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "iap.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# --- Network -----------------------------------------------------------------
#
# A custom-mode VPC, because auto-mode creates a subnet in every region with
# default firewall rules. Custom mode has an implicit deny on ingress, so the
# only way in is the IAP rule below.

resource "google_compute_network" "vpc" {
  name                    = "${local.name}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.required]
}

resource "google_compute_subnetwork" "subnet" {
  name          = "${local.name}-subnet"
  ip_cidr_range = "10.10.0.0/24"
  region        = var.region
  network       = google_compute_network.vpc.id

  # Lets the VM reach Google APIs (Secret Manager, Artifact Registry) without a
  # public IP or a NAT hop.
  private_ip_google_access = true
}

# The VM has no external IP, so without NAT it cannot reach the internet at all
# — and it must: pulling base images, cloning the repo, installing the harness
# CLIs, and every LLM API call an agent makes.
resource "google_compute_router" "router" {
  name    = "${local.name}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  name                               = "${local.name}-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# The only ingress rule. 35.235.240.0/20 is IAP's TCP-forwarding range — it is
# not "the internet", it is Google's proxy, and reaching it still requires IAM.
resource "google_compute_firewall" "iap_ssh" {
  name          = "${local.name}-allow-iap-ssh"
  network       = google_compute_network.vpc.name
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.name]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# --- Private services access, for Cloud SQL ----------------------------------

resource "google_compute_global_address" "private_ip_range" {
  name          = "${local.name}-sql-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_vpc" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_ip_range.name]
  depends_on              = [google_project_service.required]
}

# --- Database ----------------------------------------------------------------

resource "random_password" "db" {
  length  = 32
  special = false # keeps the DSN free of characters needing URL-escaping
}

resource "google_sql_database_instance" "pg" {
  name                = "${local.name}-pg"
  region              = var.region
  database_version    = var.db_version
  deletion_protection = var.db_deletion_protection
  depends_on          = [google_service_networking_connection.private_vpc]

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_size         = var.db_disk_gb
    disk_autoresize   = true
    user_labels       = var.labels

    ip_configuration {
      # No public IP. The VM reaches it over the peered VPC.
      ipv4_enabled    = false
      private_network = google_compute_network.vpc.id
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "03:00"
    }

    maintenance_window {
      day  = 7
      hour = 4
    }
  }
}

resource "google_sql_database" "db" {
  name     = local.db_name
  instance = google_sql_database_instance.pg.name
}

# AQ handles a managed database with no superuser: its install steps check
# whether the role and database already work instead of failing. It also
# requires no server-side extensions, so a restricted allowlist is fine.
resource "google_sql_user" "user" {
  name     = local.db_user
  instance = google_sql_database_instance.pg.name
  password = random_password.db.result
}

# --- Secrets -----------------------------------------------------------------
#
# The whole DSN, not split into parts — the daemon takes one AQ_DATABASE_URL.

resource "google_secret_manager_secret" "database_url" {
  secret_id = "${local.name}-database-url"
  labels    = var.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "database_url" {
  secret = google_secret_manager_secret.database_url.id
  secret_data = format(
    "postgresql+asyncpg://%s:%s@%s:5432/%s",
    local.db_user,
    random_password.db.result,
    google_sql_database_instance.pg.private_ip_address,
    local.db_name,
  )
}

# --- Identity ----------------------------------------------------------------

resource "google_service_account" "vm" {
  account_id   = "${local.name}-vm"
  display_name = "Agent Queue VM"
}

resource "google_secret_manager_secret_iam_member" "vm_reads_dsn" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# Write-only logging/metrics. Deliberately no roles/editor.
resource "google_project_iam_member" "vm_telemetry" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.vm.email}"
}

# --- The data disk -----------------------------------------------------------
#
# Separate from the instance on purpose: it is the thing that must survive.
# Every property that keeps repository cost flat — reusable worktree slots,
# warm gitignored caches, a shared object store — lives here, and the slot rows
# in PostgreSQL reference these paths. Lose it and a warm resume becomes a full
# re-clone against rows pointing at nothing.

resource "google_compute_disk" "data" {
  name   = "${local.name}-data"
  type   = var.data_disk_type
  zone   = var.zone
  size   = var.data_disk_gb
  labels = var.labels

  lifecycle {
    prevent_destroy = true
  }
}

# --- The VM ------------------------------------------------------------------

resource "google_compute_instance" "vm" {
  name         = "${local.name}-vm"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = [local.name]
  labels       = var.labels

  # Lets `terraform apply` resize the machine without being blocked by a
  # running instance — useful when moving between machine types or to SPOT.
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = var.boot_disk_gb
      type  = "pd-balanced"
    }
  }

  attached_disk {
    source      = google_compute_disk.data.id
    device_name = "aqdata"
    mode        = "READ_WRITE"
  }

  network_interface {
    subnetwork = google_compute_subnetwork.subnet.id
    # No access_config block: no external IP. Egress is via Cloud NAT, ingress
    # is IAP only.
  }

  scheduling {
    provisioning_model = var.provisioning_model
    preemptible        = var.provisioning_model == "SPOT"
    automatic_restart  = var.provisioning_model == "STANDARD"
    # A preempted VM should come back by itself; nothing else releases the
    # workspace locks it was holding (DESIGN.md §6.3).
    instance_termination_action = var.provisioning_model == "SPOT" ? "STOP" : null
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    # OS Login ties SSH access to IAM rather than to metadata keys.
    enable-oslogin = "TRUE"
    user-data = templatefile("${path.module}/cloud-init.yaml", {
      repo_url            = var.repo_url
      repo_ref            = var.repo_ref
      harnesses           = var.harnesses
      dashboard_port      = var.dashboard_port
      database_url_secret = google_secret_manager_secret.database_url.secret_id
      project_id          = var.project_id
    })
  }

  depends_on = [
    google_secret_manager_secret_version.database_url,
    google_secret_manager_secret_iam_member.vm_reads_dsn,
    google_compute_router_nat.nat,
  ]
}
