# WhatsApp AI Billing Bot

An AI-powered WhatsApp assistant that extracts, organizes, and retrieves invoice data. Users send invoices as WhatsApp messages and ask billing questions in plain language. They don't need a separate app or any manual data entry.

> Technical design: see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. Problem

Small and mid-sized businesses receive invoices as PDFs, spreadsheets, photos, and forwarded text. Staff type these into billing systems by hand, which is slow and error-prone. Later, answering questions like "how much did we pay Vendor X last quarter?" means digging through folders and chat history.

## 2. Objective

Build a WhatsApp billing assistant that:

1. Accepts invoices in any common format directly in WhatsApp.
2. Automatically extracts structured data (invoice number, dates, parties, line items, taxes, totals).
3. Stores documents and data securely in the cloud.
4. Answers natural-language questions over the user's invoice history.
5. Integrates with existing billing workflows and databases.

## 3. Target Outcomes

These are design targets, to be validated against a manual-processing baseline during the pilot.

| Metric | Target |
|---|---|
| Staff workload for invoice handling | −75% |
| Data-entry errors vs. manual processing | −95% |
| Response time for billing inquiries | −90% |
| Availability | 24/7 |

## 4. Users

| Persona | Needs |
|---|---|
| **Business owner** | Quick answers on spend, dues, and vendors from their phone |
| **Accounts / billing staff** | Bulk invoice upload, no re-typing, correct records |
| **Admin / integrator** | Export data or push it to existing ERP/accounting systems |

## 5. Scope

### In scope (v1)
- **WhatsApp invoice intake:** PDF, Excel (`.xlsx`/`.xls`/`.csv`), images (JPG/PNG/photos), and plain-text invoices.
- **AI data extraction:** invoice number, invoice/due date, vendor and customer details, line items, subtotal, tax, total, and currency.
- **Validation:** totals cross-checked against line items. Low-confidence fields are flagged, and the user is asked to confirm.
- **Smart summaries:** a per-invoice breakdown sent back after processing.
- **Semantic search and Q&A:** "Show unpaid invoices from ACME", "Total spend in March", "What did we buy from Vendor X?"
- **Secure storage:** original documents in Cloud Storage, structured data in Firestore, and embeddings in Pinecone.
- **REST API:** for integrations (list/get invoices, search, export).
- **CI/CD:** automated tests and deployment to Cloud Run.

### Out of scope (v1)
- Payment processing or sending payment links.
- Issuing invoices to third parties.
- Multi-language OCR beyond what the AI model supports natively.
- A full web dashboard (only a minimal admin view, if time allows).

## 6. Key Features

| Feature | Description |
|---|---|
| WhatsApp-based processing | Upload invoices by sending them to the business number |
| AI-powered extraction | OpenAI + LangChain structured extraction into a validated schema |
| Smart invoice analysis | Summaries, line-item breakdowns, and tax/total checks |
| Multi-format support | PDF, Excel/CSV, images, and text |
| Secure cloud integration | GCS, Firestore, and Secret Manager on Google Cloud |
| Semantic search | Natural-language retrieval over invoice history (RAG with Pinecone) |

## 7. Technology Stack

| Layer | Technology |
|---|---|
| Messaging | WhatsApp Business (Cloud) API |
| AI / NLP | OpenAI (LLM, vision, embeddings), LangChain |
| Backend | Python 3.13, FastAPI, Pydantic v2 |
| Structured data | Google Firestore |
| Vector store | Pinecone |
| File storage | Google Cloud Storage |
| Async processing | Google Cloud Tasks |
| Secrets | Google Secret Manager |
| Deployment | Docker, Google Cloud Run |
| CI/CD | GitHub Actions |

## 8. Milestones

| # | Milestone | Deliverables |
|---|---|---|
| M1 | Foundations | Repo scaffold, FastAPI app, Dockerfile, CI pipeline, GCP project setup |
| M2 | WhatsApp integration | Webhook verification, signature check, receive and send messages, media download |
| M3 | Document ingestion | GCS upload, parsers for PDF/Excel/image/text, async job queue |
| M4 | AI extraction | Extraction schema, LLM pipeline, validation, confirmation flow, Firestore persistence |
| M5 | Semantic search | Chunking, embeddings, Pinecone indexing, RAG Q&A, structured-query tools |
| M6 | Integration API | REST endpoints, API keys, CSV export |
| M7 | Hardening and launch | Observability, rate limits, load testing, security review, pilot rollout |

## 9. Success Criteria

- ≥ 95% field-level accuracy on a labelled test set of 200+ real invoices.
- Median time from upload to summary reply < 20 s. Text query replies < 5 s.
- Zero cross-user data leakage, verified by automated tenant-isolation tests.
- Webhook acknowledged in < 1 s (well within WhatsApp's retry window).
- Unit and integration test coverage ≥ 80% on core modules.

## 10. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Poor-quality photos reduce extraction accuracy | Vision model with image preprocessing. Low-confidence fields go to user confirmation. |
| LLM hallucination of values | Strict schema, arithmetic validation, answers grounded only in retrieved data |
| WhatsApp policy limits (24-hour session window, template rules) | Pre-approved message templates for out-of-window notifications |
| LLM / API cost growth | Per-user rate limits, caching, cheaper models for classification |
| Sensitive financial data exposure | Per-tenant isolation, encryption at rest, least-privilege IAM, no PII in logs |

## 11. Repository Layout

```
.
├── app/
│   ├── main.py                # FastAPI entrypoint
│   ├── container.py           # Wires services; swaps backends via settings
│   ├── cli.py                 # Admin commands + local WhatsApp simulator
│   ├── api/                   # Routers: webhook, tasks (workers), invoices (REST), auth deps
│   ├── whatsapp/              # Cloud API client, webhook payload models
│   ├── conversation/          # Message routing, corrections, reply formatting
│   ├── services/              # Invoice lifecycle, background workers
│   ├── ingestion/             # GCS storage, format parsers (PDF, Excel/CSV, image, text)
│   ├── extraction/            # Prompts, business-rule validation
│   ├── ai/                    # OpenAI/LangChain provider + offline fake
│   ├── search/                # Query planner, analytics, vector store, indexer
│   ├── repositories/          # Firestore + in-memory data access
│   ├── core/                  # Config, logging, security, dates, errors
│   └── models/                # Pydantic domain models
├── tests/
│   ├── unit/
│   └── integration/           # Webhook→reply end-to-end, REST, tasks, repository contract
├── infra/                     # Terraform (Cloud Run, Firestore, GCS, Tasks, IAM, WIF)
├── .github/workflows/         # CI and CD pipelines
├── Dockerfile
├── pyproject.toml
├── README.md
├── PROJECT.md
└── ARCHITECTURE.md
```
