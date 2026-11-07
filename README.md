# WhatsApp AI Billing Bot

An AI assistant that runs in WhatsApp. Users send it invoices as PDFs, Excel/CSV files, photos or plain text. It extracts and validates the data, stores it, and answers billing questions in natural language, such as *"How much did we spend with ACME last quarter?"*

- Product scope and goals: [PROJECT.md](PROJECT.md)
- Technical design: [ARCHITECTURE.md](ARCHITECTURE.md)

## Quick start (offline, no API keys)

Every external service has an in-memory or heuristic stand-in, so the app runs locally without any credentials.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

# 1. Chat with the bot in your terminal (simulated WhatsApp)
python -m app.cli chat
#   🧑 help
#   🧑 /file path/to/invoice.pdf
#   🧑 How much did I spend last month?
#   🧑 /tap 1                      ← press a reply button

# 2. Or run the HTTP server (API docs at http://localhost:8080/docs)
uvicorn app.main:app --reload --port 8080
#   In /docs click "Authorize", enter dev-key-123, then "Try it out" on any /v1 endpoint.
#   (Local in-memory mode only; data resets when the server restarts.)

# 3. Tests, lint, types
pytest
ruff check app tests
mypy app
```

> The offline AI (`AI_BACKEND=fake`) uses regex heuristics. It handles clean text invoices but **can't read photos or scanned PDFs**, which go to manual review. Set `AI_BACKEND=openai` for real extraction.

## Running against real services

Copy `.env.example` to `.env`, then switch backends one at a time:

| Setting | Offline default | Production | Needs |
|---|---|---|---|
| `AI_BACKEND` | `fake` | `openai` | `OPENAI_API_KEY` |
| `WHATSAPP_BACKEND` | `fake` | `cloud` | `WHATSAPP_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_PHONE_NUMBER_ID` |
| `STORAGE_BACKEND` | `memory` | `gcp` (Firestore + GCS) | `GCP_PROJECT`, `GCS_BUCKET`, Application Default Credentials |
| `VECTOR_BACKEND` | `memory` | `pinecone` | `PINECONE_API_KEY`, a serverless index (dimension 1536, cosine) |
| `TASK_BACKEND` | `inline` | `cloudtasks` | Queue and service account (see `infra/`) |

To test WhatsApp locally, expose the server with a tunnel (e.g. `ngrok http 8080`). In **Meta App Dashboard → WhatsApp → Configuration**, set the callback URL to `https://<tunnel>/webhook` and the verify token to `WHATSAPP_VERIFY_TOKEN`, then subscribe to the `messages` field.

## Admin CLI

```bash
python -m app.cli create-tenant --name "Lucky Traders" --currency INR --wa-id 919876543210
python -m app.cli add-user --tenant-id <id> --wa-id 919811112222
python -m app.cli create-api-key --tenant-id <id> --name erp-sync     # for the REST API
python -m app.cli reconcile-index                                     # re-index failed vector upserts
```

With `AUTO_PROVISION_TENANTS=true` (the local default), any new WhatsApp sender gets their own tenant. In production, set it to `false` and register numbers with the CLI.

## What the bot understands

| User sends | Bot does |
|---|---|
| PDF / Excel / CSV / photo / pasted invoice text | Extracts the data, validates totals and dates, saves it, and replies with a summary |
| Uncertain or inconsistent invoice | Asks for review with **Confirm / Discard** buttons |
| `total is 1250, due date 2026-03-31` | Corrects the pending invoice and re-validates it |
| `mark INV-1001 as paid` | Updates the payment status |
| `How much did I spend in March?` · `spend by vendor this year` · `how many invoices from ACME?` | Exact aggregates from the database |
| `show unpaid invoices` | Filtered list |
| `find invoice no G-77` | Invoice details |
| `which invoice had toner cartridges?` | Semantic search (RAG), with the answer grounded in the matching records |

## REST API (`X-API-Key` header)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/invoices?vendor=&date_from=&date_to=&payment_status=&status=` | List or filter invoices |
| `POST` | `/v1/invoices` (multipart `file`) | Upload an invoice |
| `GET` | `/v1/invoices/{id}` | Invoice details plus a signed document URL |
| `PATCH` | `/v1/invoices/{id}` | Correct fields, `payment_status`, `confirm: true` |
| `DELETE` | `/v1/invoices/{id}` | Delete an invoice |
| `POST` | `/v1/search` `{"question": "..."}` | Natural-language Q&A |
| `GET` | `/v1/exports/invoices.csv` | CSV export |

## Deploying to Google Cloud

1. `cd infra && cp example.tfvars staging.tfvars`, edit the values, then `terraform init && terraform apply -var-file=staging.tfvars`.
2. Add the secret values:
   ```bash
   printf '%s' "$VALUE" | gcloud secrets versions add whatsapp-token --data-file=-
   # repeat for whatsapp-app-secret, whatsapp-verify-token, openai-api-key, pinecone-api-key
   ```
3. Create the Pinecone serverless index `invoices` (dimension 1536, cosine).
4. In GitHub, create the `staging` and `production` environments with the variables `GCP_PROJECT`, `GCP_REGION`, `WIF_PROVIDER` and `DEPLOY_SERVICE_ACCOUNT` (from the Terraform outputs). Add required reviewers to `production`.
5. Push to `main`. CI runs the lint, type and test checks plus a Firestore emulator and a Docker smoke test. Then **Deploy** builds the image and ships it to staging, and promotes it to production after approval.
6. Point the Meta webhook at the `webhook_url` Terraform output.

## Project layout

```
app/
  main.py              FastAPI app, middleware, error handlers
  container.py         Wires services from settings (swap backends here)
  cli.py               Admin commands + local WhatsApp simulator
  api/                 webhook · tasks (Cloud Tasks workers) · invoices (REST) · deps (auth)
  whatsapp/            Webhook payload parsing, Cloud API client (+ fake)
  conversation/        Message routing, corrections, WhatsApp formatting
  services/            Invoice lifecycle, background workers
  ingestion/           GCS storage, parsers (PDF, Excel/CSV, image, text)
  extraction/          Prompts, business-rule validation
  ai/                  OpenAI/LangChain provider (+ offline fake)
  search/              Query planner, analytics, Pinecone/vector store, indexer
  repositories/        Firestore and in-memory data access
  models/              Pydantic domain models
tests/                 Unit + integration (webhook→reply end-to-end, REST, tasks, repo contract)
infra/                 Terraform: Cloud Run, Firestore, GCS, Tasks, Scheduler, IAM, WIF
.github/workflows/     CI and CD pipelines
```
