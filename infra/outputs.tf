output "service_url" {
  value = local.service_url
}

output "webhook_url" {
  value       = "${local.service_url}/webhook"
  description = "Paste into Meta App Dashboard → WhatsApp → Configuration → Webhook"
}

output "documents_bucket" {
  value = google_storage_bucket.documents.name
}

output "workload_identity_provider" {
  value       = google_iam_workload_identity_pool_provider.github.name
  description = "GitHub Actions variable WIF_PROVIDER"
}

output "deploy_service_account" {
  value       = google_service_account.deployer.email
  description = "GitHub Actions variable DEPLOY_SERVICE_ACCOUNT"
}
