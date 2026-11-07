variable "project_id" {
  type        = string
  description = "GCP project for this environment"
}

variable "environment" {
  type        = string
  description = "dev | staging | prod"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "firestore_location" {
  type    = string
  default = "nam5"
}

variable "service_name" {
  type    = string
  default = "billing-bot"
}

variable "image" {
  type        = string
  description = "Initial container image (later deploys come from CI/CD)"
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "whatsapp_phone_number_id" {
  type = string
}

variable "pinecone_index" {
  type    = string
  default = "invoices"
}

variable "github_repository" {
  type        = string
  description = "owner/repo allowed to deploy via Workload Identity Federation"
}

variable "min_instances" {
  type    = number
  default = 1
}

variable "max_instances" {
  type    = number
  default = 10
}

variable "concurrency" {
  type    = number
  default = 20
}

variable "tasks_max_dispatches_per_second" {
  type        = number
  default     = 5
  description = "Keep below your OpenAI rate limits"
}

variable "nearline_after_days" {
  type    = number
  default = 90
}

variable "auto_provision_tenants" {
  type    = bool
  default = false
}
