# Development Plan

Companion to [ARCHITECTURE.md](ARCHITECTURE.md). Work proceeds one phase at a time; each phase ends
with tests passing and a short change summary before the next phase starts. Existing files are
inspected before any change, and nothing is overwritten without confirmation.

## Phase 0 — Inspection & architecture ✅ (this phase)
- Inspected folder: empty `backend/ frontend/ infrastructure/ docs/ tests/`, empty `README.md.txt`,
  `.env.example` exists as a **directory**; no git repo.
- Tooling found: Python 3.11.1, Node 24.12, Docker 29.7, Git 2.51.
- Deliverables: `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT_PLAN.md`.

## Phase 1 — Project scaffolding ✅
- `git init`, `.gitignore`, `.editorconfig`, pre-commit (ruff lint + format; Prettier/ESLint arrive with the frontend in Phase 9).
- Custom `accounts.User` (UUID pk, email login) created before the first migration; roles/2FA in Phase 2.
- Replace `.env.example` directory with a file; rename `README.md.txt` → `README.md` (with confirmation).
- Django project `backend/config` with split settings (base/dev/test/prod), env via `django-environ`.
- Docker Compose **dev** stack: MySQL 8, Redis, web, worker, beat.
- Celery app wiring, health endpoints, structured logging with PII filter.
- **Exit:** `docker compose up` boots; `pytest` runs a smoke test; `/healthz` returns 200.

## Phase 2 — Core: security primitives, users, audit ✅
- `core`: `EncryptedTextField` (MultiFernet), blind-index helper, key-rotation command, base model (UUID, timestamps),
  startup checks on keys, shared redaction rules.
- `accounts`: roles + capability matrix, permission classes, TOTP 2FA, session auth API
  (`/api/v1/auth/{csrf,login,logout,me,2fa/setup,2fa/confirm,2fa/verify}`), per-IP throttle + per-account lockout.
- `audit`: append-only `AuditLog` with hash chain, `verify_audit_chain` command, request-id middleware.
- **Exit:** tests for encryption round-trip/rotation, blind index lookup, audit immutability, role checks.

## Phase 3 — Domain models & migrations ✅
- `customers`, `reports`, `payments`, `followups`, `messaging`, `conversations`, `WebhookEvent`.
- Status enums kept separate; `active_lock` unique constraint; all unique external IDs.
- Django admin (read-only, 2FA-protected, PII masked) for debugging.
- Shared factory_boy factories in `apps/core/testing/factories.py`.
- **Exit:** migrations apply on MySQL; model tests for constraints (duplicate active request rejected, etc.).

## Phase 4 — Service layer & adapters (mocks) ✅
- Adapter interfaces + `Mock*` implementations for provider, WhatsApp, Razorpay.
- Services: `ReportService` (initiate OTP, verify, fetch, store PDF), `PaymentService`,
  `FollowUpService`, `MessagingService` (24-h window logic), `ReportStorage` (encrypted private files).
- Celery tasks with typed retries and queues.
- **Exit:** full happy path + failure paths run end-to-end against mocks in tests.
- Done: adapters in `reports/providers`, `messaging/gateway`, `payments/gateway` (mock scenarios are
  documented in each `mock.py`); services in each app's `services.py`; tasks in `reports/tasks.py` and
  `payments/tasks.py`, routed by `CELERY_TASK_ROUTES`. Verified end to end through the live worker in Docker.

## Phase 5 — Backend REST APIs ✅
- DRF viewsets per §8 of the architecture, filters, pagination, OpenAPI schema.
- Object-level permissions, PII masking/reveal, audited PDF download.
- **Exit:** API tests for every endpoint × role matrix; OpenAPI schema generated.
- Done: 49 operations under `/api/v1/` (`config/api_urls.py`); schema at `/api/v1/schema`, Swagger UI at
  `/api/v1/docs` (staff only), exported to `docs/openapi.yaml`. Settings are read-only for now; editing them
  at runtime needs a DB-backed settings model (to schedule with the dashboard work).

## Phase 6 — Razorpay integration
- Real `RazorpayGateway` using official SDK (test mode), payment-link creation/resend/cancel.
- Webhook endpoint: raw-body signature check, `WebhookEvent` dedupe, async processing,
  amount/currency verification, reconciliation job, refunds (request → approve → process).
- **Needs:** Razorpay test keys + webhook secret.
- **Exit:** idempotency/replay/concurrency tests; manual test with Razorpay test mode.

## Phase 7 — WhatsApp integration & conversation bot
- Real `CloudApiWhatsAppGateway`: text, interactive buttons, templates, media upload, documents.
- Webhook: verify-token handshake, `X-Hub-Signature-256` check, dedupe, status updates → `MessageLog`.
- Conversation state machine, validation, global commands, OTP redaction path.
- **Needs:** WABA, phone number ID, access token, app secret, approved templates.
- **Exit:** state-machine unit tests; OTP-never-persisted tests; manual test on a test number.

## Phase 8 — Celery reminders, escalation & scheduled jobs
- Beat schedule per §11; reminder dispatch with `skip_locked`; escalation; auto-close on payment;
  stale-request expiry; PDF purge; daily aggregates.
- **Exit:** tests with `freezegun` for cadence, no double send, stop conditions.

## Phase 9 — React admin dashboard
- Vite + Tailwind scaffold, auth/2FA, layout, role-aware navigation.
- Pages in order: Follow-ups queue (highest business value) → Report requests → Payments/Refunds →
  Customers → Messages → Dashboard/Analytics → Staff → Audit → Settings.
- **Exit:** Vitest/RTL tests for key flows; production build served by Nginx locally.

## Phase 10 — Real provider adapter (when docs are available)
- Implement the authorised partner's adapter strictly from its documentation; contract tests against its sandbox.
- Can run in parallel with Phases 8–9 once documentation arrives.

## Phase 11 — Production deployment
- Prod Dockerfiles (multi-stage, non-root), `docker-compose.prod.yml`, Nginx + TLS, UFW,
  backups, log rotation, Sentry, runbook (`docs/RUNBOOK.md`), deployment checklist.
- Security review: headers, secrets, dependency audit (`pip-audit`, `npm audit`), OWASP checks.
- **Exit:** staging deploy on the VPS with test-mode keys; go-live checklist signed off.

## Definition of done (every phase)
1. Code + migrations reviewed against the architecture doc (doc updated if the design changed).
2. `pytest` (and frontend tests where relevant) passing; lint clean.
3. Change summary: files added/changed, decisions, anything deferred.
