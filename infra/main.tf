# One environment (dev / staging / prod) per GCP project:
#   cd infra && terraform init && terraform apply -var-file=staging.tfvars

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {}

locals {
  # Cloud Run's deterministic URL lets the service know its own address (Cloud Tasks targets/audience).
  service_url = "https://${var.service_name}-${data.google_project.this.number}.${var.region}.run.app"
  secrets     = ["whatsapp-token", "whatsapp-app-secret", "whatsapp-verify-token", "openai-api-key", "pinecone-api-key"]
}

# ---- APIs ----------------------------------------------------------------------------------
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "firestore.googleapis.com",
    "cloudtasks.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "iamcredentials.googleapis.com",
    "storage.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# ---- Service accounts -----------------------------------------------------------------------
resource "google_service_account" "runtime" {
  account_id   = "${var.service_name}-run"
  display_name = "Billing bot runtime"
}

resource "google_service_account" "tasks" {
  account_id   = "${var.service_name}-tasks"
  display_name = "Billing bot Cloud Tasks / Scheduler invoker"
}

resource "google_project_iam_member" "runtime_roles" {
  for_each = toset([
    "roles/datastore.user",
    "roles/cloudtasks.enqueuer",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/monitoring.metricWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# Runtime may create tasks that carry the tasks SA's OIDC identity.
resource "google_service_account_iam_member" "runtime_act_as_tasks" {
  service_account_id = google_service_account.tasks.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.runtime.email}"
}

# Runtime signs GCS URLs via IAM signBlob on itself.
resource "google_service_account_iam_member" "runtime_sign_self" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.runtime.email}"
}

# ---- Storage --------------------------------------------------------------------------------
resource "google_storage_bucket" "documents" {
  name                        = "${var.project_id}-invoice-documents"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    condition { age = var.nearline_after_days }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "runtime_objects" {
  bucket = google_storage_bucket.documents.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# ---- Firestore ------------------------------------------------------------------------------
resource "google_firestore_database" "default" {
  name        = "(default)"
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.apis]
}

resource "google_firestore_field" "ttl" {
  for_each   = toset(["processed_messages", "sessions"])
  database   = google_firestore_database.default.name
  collection = each.value
  field      = "expire_at"
  ttl_config {}
}

resource "google_firestore_index" "invoices" {
  for_each = {
    status_date  = [["status", "ASCENDING"], ["invoice_date", "DESCENDING"]]
    payment_date = [["payment_status", "ASCENDING"], ["invoice_date", "DESCENDING"]]
    both_date    = [["status", "ASCENDING"], ["payment_status", "ASCENDING"], ["invoice_date", "DESCENDING"]]
    vendor_no    = [["vendor_key", "ASCENDING"], ["invoice_number", "ASCENDING"]]
  }
  database    = google_firestore_database.default.name
  collection  = "invoices"
  query_scope = "COLLECTION"

  dynamic "fields" {
    for_each = each.value
    content {
      field_path = fields.value[0]
      order      = fields.value[1]
    }
  }
}

resource "google_firestore_index" "unindexed_group" {
  database    = google_firestore_database.default.name
  collection  = "invoices"
  query_scope = "COLLECTION_GROUP"
  fields {
    field_path = "indexed"
    order      = "ASCENDING"
  }
  fields {
    field_path = "status"
    order      = "ASCENDING"
  }
}

# ---- Secrets (values are added out-of-band: gcloud secrets versions add ...) ----------------
resource "google_secret_manager_secret" "secrets" {
  for_each  = toset(local.secrets)
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "runtime_access" {
  for_each  = google_secret_manager_secret.secrets
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

# ---- Cloud Tasks ----------------------------------------------------------------------------
resource "google_cloud_tasks_queue" "jobs" {
  name     = "invoice-jobs"
  location = var.region

  rate_limits {
    max_dispatches_per_second = var.tasks_max_dispatches_per_second
    max_concurrent_dispatches = 20
  }
  retry_config {
    max_attempts  = 5 # keep in sync with app/api/tasks.py MAX_ATTEMPTS
    min_backoff   = "5s"
    max_backoff   = "300s"
    max_doublings = 4
  }
  depends_on = [google_project_service.apis]
}

# ---- Artifact Registry ----------------------------------------------------------------------
resource "google_artifact_registry_repository" "images" {
  repository_id = var.service_name
  location      = var.region
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# ---- Cloud Run ------------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "bot" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL" # Meta must reach /webhook

  template {
    service_account                  = google_service_account.runtime.email
    timeout                          = "300s"
    max_instance_request_concurrency = var.concurrency

    scaling {
      min_instance_count = var.min_instances # avoid cold starts on the webhook
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image
      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }

      dynamic "env" {
        for_each = {
          ENVIRONMENT              = var.environment
          PUBLIC_BASE_URL          = local.service_url
          STORAGE_BACKEND          = "gcp"
          VECTOR_BACKEND           = "pinecone"
          TASK_BACKEND             = "cloudtasks"
          AI_BACKEND               = "openai"
          WHATSAPP_BACKEND         = "cloud"
          GCP_PROJECT              = var.project_id
          GCS_BUCKET               = google_storage_bucket.documents.name
          TASKS_QUEUE              = google_cloud_tasks_queue.jobs.name
          TASKS_LOCATION           = var.region
          TASKS_SERVICE_ACCOUNT    = google_service_account.tasks.email
          TASKS_AUDIENCE           = local.service_url
          WHATSAPP_PHONE_NUMBER_ID = var.whatsapp_phone_number_id
          PINECONE_INDEX           = var.pinecone_index
          AUTO_PROVISION_TENANTS   = tostring(var.auto_provision_tenants)
        }
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = {
          WHATSAPP_TOKEN        = "whatsapp-token"
          WHATSAPP_APP_SECRET   = "whatsapp-app-secret"
          WHATSAPP_VERIFY_TOKEN = "whatsapp-verify-token"
          OPENAI_API_KEY        = "openai-api-key"
          PINECONE_API_KEY      = "pinecone-api-key"
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secrets[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get { path = "/healthz" }
      }
      liveness_probe {
        http_get { path = "/healthz" }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image] # CI/CD deploys new images
  }
  depends_on = [google_secret_manager_secret_iam_member.runtime_access]
}

# Public: WhatsApp webhooks are authenticated by HMAC signature; /tasks/* by OIDC in-app;
# /v1/* by API key.
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.bot.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---- Scheduler: periodic vector re-index of anything that failed to index ------------------
resource "google_cloud_scheduler_job" "reconcile" {
  name     = "${var.service_name}-reconcile-index"
  region   = var.region
  schedule = "*/30 * * * *"

  http_target {
    http_method = "POST"
    uri         = "${local.service_url}/tasks/reconcile-index"
    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = local.service_url
    }
  }
  depends_on = [google_project_service.apis]
}

# ---- GitHub Actions deploy identity (Workload Identity Federation) -------------------------
resource "google_service_account" "deployer" {
  account_id   = "${var.service_name}-deployer"
  display_name = "GitHub Actions deployer"
}

resource "google_project_iam_member" "deployer_roles" {
  for_each = toset(["roles/run.developer", "roles/artifactregistry.writer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_act_as_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == \"${var.github_repository}\""
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}
