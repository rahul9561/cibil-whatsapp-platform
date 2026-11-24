# WhatsApp CIBIL Report Platform

WhatsApp-based credit report request, payment (Razorpay) and follow-up management platform with a
React admin dashboard.

- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Development plan & phase status: [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)

## Layout

```
backend/          Django + DRF + Celery (apps/, config/)
frontend/         React admin (Phase 9)
infrastructure/   Dockerfiles, compose files, nginx
docs/             Architecture and plans
tests/            Cross-cutting e2e / contract tests
```

## Quick start (Docker)

```bash
cp .env.example .env          # then replace every "change-me" value
docker compose -f infrastructure/compose/docker-compose.dev.yml --env-file .env up --build
```

<!-- docker compose -f infrastructure/compose/docker-compose.dev.yml exec web python manage.py seed_dev_staff -->

<!-- docker compose -f infrastructure/compose/docker-compose.dev.yml --env-file .env exec web python manage.py createsuperuser -->

- API: http://localhost:8000 — `GET /healthz` (liveness), `GET /readyz` (MySQL + Redis)
- MySQL is reachable from the host on `127.0.0.1:3307` for DB tools.

## Backend without Docker (tests use SQLite by default)

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements/dev.txt   # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m pytest
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .
```

To run tests against MySQL instead, set `DATABASE_URL=mysql://...` before `pytest`, or run them inside
the dev stack:

```bash
docker compose -f infrastructure/compose/docker-compose.dev.yml --env-file .env exec -e DJANGO_SETTINGS_MODULE=config.settings.test web python -m pytest -p no:cacheprovider
```

## Admin dashboard (frontend/)

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api to the backend on :8000
npm test           # Vitest + Testing Library
npm run build      # production build in frontend/dist
```

With the Docker stack running, the production build is served by nginx at http://localhost:8080
(`npm run build` first). Local logins: `docker compose ... exec web python manage.py seed_dev_staff --demo-data`
creates one account per role (dev only; the shared password is in that command's source).

## Settings

`config.settings.{dev,test,prod}`; selected with `DJANGO_SETTINGS_MODULE`. All secrets come from
environment variables (see `.env.example`). Never commit `.env`.
