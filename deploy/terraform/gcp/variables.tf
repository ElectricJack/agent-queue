# Inputs. Only project_id has no sensible default — everything else is tuned
# for the target in deploy/DESIGN.md §8: one ordinary VM, one persistent disk,
# managed PostgreSQL on a private IP, no public ingress.

variable "project_id" {
  description = "GCP project ID to deploy into."
  type        = string
}

variable "region" {
  description = "Region for the network, Cloud SQL and NAT."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zone for the VM and its data disk. Must be inside var.region."
  type        = string
  default     = "us-central1-a"
}

variable "name" {
  description = "Name prefix for every resource, so several stacks can coexist."
  type        = string
  default     = "agent-queue"
}

# --- The box -----------------------------------------------------------------

variable "machine_type" {
  description = <<-EOT
    Sized for ~4 concurrent agents. The binding constraint is what agents
    *spawn* — builds and test suites — not the agents themselves, which measure
    around 200 MB resident each. Size for your projects, not the agent count.
  EOT
  type        = string
  default     = "e2-standard-4"
}

variable "provisioning_model" {
  description = <<-EOT
    STANDARD or SPOT. Step 1 of DESIGN.md §8 is deliberately STANDARD: the
    unknowns in a first deploy are infrastructure, not the application, and
    debugging those alongside preemption is a bad trade. Flipping to SPOT later
    is this one variable — same disk, same image, same DSN.

    Before flipping, read §6.3: something must restart the daemon after a
    preemption, because workspace locks are released *by the daemon restarting*.
  EOT
  type        = string
  default     = "STANDARD"

  validation {
    condition     = contains(["STANDARD", "SPOT"], var.provisioning_model)
    error_message = "provisioning_model must be STANDARD or SPOT."
  }
}

variable "boot_disk_gb" {
  description = "Boot disk. Holds the OS and container images, not agent data."
  type        = number
  default     = 50
}

variable "data_disk_gb" {
  description = <<-EOT
    The disk that actually matters. It holds the vault, the base clones and
    every worktree slot — and slots keep their gitignored caches by design, so
    the driver is N_slots x (working tree + node_modules/.venv/target), not the
    repository size. pd-balanced resizes online, so start here and grow.
  EOT
  type        = number
  default     = 100
}

variable "data_disk_type" {
  description = <<-EOT
    MUST be a network-attached disk (pd-balanced, pd-ssd, hyperdisk-balanced).
    Never Local SSD: it is physically attached and is wiped on preemption,
    which would destroy every worktree slot while leaving the database rows
    pointing at paths that no longer exist. See DESIGN.md §6.2.
  EOT
  type        = string
  default     = "pd-balanced"

  validation {
    condition     = !can(regex("local", var.data_disk_type))
    error_message = "Local SSD does not survive preemption; use pd-balanced, pd-ssd or hyperdisk-balanced."
  }
}

variable "egress_mode" {
  description = <<-EOT
    How the VM reaches the internet. It must reach it: pulling images, cloning
    the repo, installing harness CLIs, and every LLM call an agent makes.

    "external_ip" (default) attaches an ephemeral public IPv4, roughly $3/month.
    "nat" routes through Cloud NAT with no public address at all, roughly
    $32/month for the gateway plus per-GB processing.

    Ingress is denied either way — the custom VPC has an implicit deny and the
    only rule allows IAP's range to port 22 — and the dashboard binds to
    127.0.0.1 *on the VM*, so it is not listening on a public interface
    regardless. The difference is defence in depth: with NAT there is no
    inbound path to misconfigure, while "external_ip" leaves the firewall as
    the thing that must stay correct.

    Given the web layer has effectively no authentication (deploy/README.md),
    choose "nat" if you would rather pay for the extra layer.
  EOT
  type        = string
  default     = "external_ip"

  validation {
    condition     = contains(["external_ip", "nat"], var.egress_mode)
    error_message = "egress_mode must be external_ip or nat."
  }
}

# --- Database ----------------------------------------------------------------

variable "db_tier" {
  description = "Cloud SQL machine tier. db-g1-small is fine to start."
  type        = string
  default     = "db-g1-small"
}

variable "db_version" {
  description = "Cloud SQL PostgreSQL version. AQ requires PostgreSQL and needs no extensions."
  type        = string
  default     = "POSTGRES_17"
}

variable "db_disk_gb" {
  description = "Cloud SQL storage. Autoresize is enabled, so this is a floor."
  type        = number
  default     = 20
}

variable "db_deletion_protection" {
  description = "Keep true outside throwaway environments."
  type        = bool
  default     = true
}

# --- Application -------------------------------------------------------------

variable "repo_url" {
  description = "Repository the VM clones to get deploy/ at boot."
  type        = string
  default     = "https://github.com/ElectricJack/agent-queue.git"
}

variable "repo_ref" {
  description = "Branch or tag to check out."
  type        = string
  default     = "main"
}

variable "harnesses" {
  description = <<-EOT
    Harness CLIs baked into the daemon image. Keep codex: the shipped
    `pr-merger` profile declares `harness: codex`, and a missing harness fails
    in the worst way available — an empty start-stderr.log, because the real
    error goes to the tmux pane. See deploy/README.md.
  EOT
  type        = string
  default     = "claude,codex"
}

variable "dashboard_port" {
  description = "Host port for the dashboard, bound to localhost on the VM and reached over IAP."
  type        = number
  default     = 8088
}

variable "labels" {
  description = "Labels applied to every resource that accepts them."
  type        = map(string)
  default     = { app = "agent-queue" }
}
