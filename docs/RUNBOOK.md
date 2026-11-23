# Operations runbook

Single Ubuntu VPS running the Docker stack in `infrastructure/compose/docker-compose.prod.yml`.
All commands run from the repository root on the server (e.g. `/opt/cibil`). Shorthand used below:

```bash
alias dc='docker compose -f infrastructure/compose/docker-compose.prod.yml --env-file .env.prod'
```

| Service | Role | Notes |
|---|---|---|
| `nginx` | TLS, security headers, rate limits, serves the admin SPA, proxies `/api`, `/webhooks`, `/django-admin` | only service with public ports (80, 443) |
| `web` | Django + Gunicorn | `/healthz` liveness; `/readyz` is internal only |
| `worker` | Celery (all queues) | stateless; scale with `CELERY_CONCURRENCY` |
| `beat` | Celery scheduler | **exactly one** instance |
| `mysql`, `redis` | data, broker/locks | internal network only, no published ports |
| `migrate` | one-off migrations (profile `tools`) | `dc run --rm migrate` |

---

## 1. Server preparation (once)

1. Ubuntu 24.04 LTS, at least 2 vCPU / 4 GB RAM / 40 GB disk. Point the domain's DNS A/AAAA records at it.
2. Create a deploy user with sudo; SSH keys only:
   `PasswordAuthentication no`, `PermitRootLogin no` in `/etc/ssh/sshd_config`, then `systemctl reload ssh`.
3. Firewall: `ufw default deny incoming && ufw allow OpenSSH && ufw allow 80,443/tcp && ufw enable`.
   Docker bypasses ufw for published ports; only nginx publishes ports, by design.
4. Install Docker Engine + compose plugin (docs.docker.com/engine/install/ubuntu), `certbot`, `unattended-upgrades`.
5. Time zone stays UTC on the host (the app converts to IST itself).

## 2. First deploy

1. `git clone` the repository to `/opt/cibil`; `cd /opt/cibil`.
2. Create `.env.prod` from `.env.example` (`chmod 600 .env.prod`). Generate every secret fresh (commands are in
   `.env.example`). Production values:
   - `DJANGO_ALLOWED_HOSTS=<domain>`, `DJANGO_CSRF_TRUSTED_ORIGINS=https://<domain>`, `DOMAIN=<domain>`
   - real `REPORT_PRICE_PAISE`; `PAYMENT_GATEWAY=razorpay`, `WHATSAPP_GATEWAY=cloud_api` with their secrets
   - `CREDIT_PROVIDER` = the real provider adapter (until Phase 10 lands, the app refuses to start with `mock`
     unless `ALLOW_MOCK_ADAPTERS=true`, which must never be set for real customers)
   - optional `SENTRY_DSN`
3. **Store a copy of `.env.prod` in the password manager now.** The encryption keys in it are the only way to
   read customer data and report files; backups are useless without them.
4. Build and migrate: `dc build && dc run --rm migrate`
5. TLS: `sudo infrastructure/scripts/init-letsencrypt.sh <domain> <ops-email>` (starts nginx, obtains the
   certificate, installs the renewal reload hook).
6. Start everything: `dc up -d`, then check: `dc ps` (all healthy), `curl -I https://<domain>/healthz`.
7. First Super Admin: `dc exec web python manage.py createsuperuser` (email + password), then sign in at
   `https://<domain>/` and enrol 2FA. Create other staff from the Staff page.
8. Register webhooks:
   - Razorpay Dashboard → Webhooks: `https://<domain>/webhooks/razorpay`, secret = `RAZORPAY_WEBHOOK_SECRET`,
     events `payment_link.paid`, `payment_link.expired`, `payment_link.cancelled`, `refund.processed`, `refund.failed`.
   - Meta app → WhatsApp → Configuration: callback `https://<domain>/webhooks/whatsapp`, verify token =
     `WHATSAPP_VERIFY_TOKEN`, subscribe to `messages`.
9. Backups (section 4) and monitoring (section 6) before taking real traffic. Then work through
   `docs/GO_LIVE_CHECKLIST.md`.

## 3. Routine deploy and rollback

```bash
cd /opt/cibil && git fetch && git checkout <release-tag>
IMAGE_TAG=<release-tag> dc build
infrastructure/scripts/backup.sh                 # always before migrating
IMAGE_TAG=<release-tag> dc run --rm migrate
IMAGE_TAG=<release-tag> dc up -d                 # recreates web/worker/beat/nginx
dc ps && curl -fsS https://<domain>/healthz
```

Rollback: `IMAGE_TAG=<previous-tag> dc up -d`. Migrations are written to be backward compatible within a
release; if a release *did* change the schema incompatibly, restore the pre-deploy backup (section 4).
Celery tasks are idempotent and webhooks are re-tried, so a restart mid-flow does not lose payments.

## 4. Backups and restore

- Nightly, as root (crontab): `15 2 * * * cd /opt/cibil && infrastructure/scripts/backup.sh >> /var/log/cibil-backup.log 2>&1`
- Needs `/etc/cibil/backup.pass` (random passphrase, `chmod 600`, **also stored off-server**). Optional
  off-site copy with `BACKUP_RCLONE_REMOTE`. Archives are AES-256 encrypted and verified after writing.
- Restore the database: `infrastructure/scripts/restore.sh /var/backups/cibil/db-<ts>.sql.gz.enc --yes`
  (stops the app, restores, verifies the audit chain, restarts).
- Restore report files: `openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:/etc/cibil/backup.pass -in reports-<ts>.tar.gz.enc | docker run --rm -i -v cibil_private_reports:/data alpine tar xzf - -C /data`
- **Test a restore every quarter** on a scratch server. An untested backup is a hope, not a backup.

## 5. Keys and secrets

| Secret | Rotation |
|---|---|
| `FIELD_ENCRYPTION_KEYS` | prepend a new Fernet key, `dc up -d`, `dc exec web python manage.py rotate_encryption_keys`, then remove the old key and `dc up -d` |
| `REPORT_FILE_ENCRYPTION_KEYS` | prepend `k2:<new>`; new files use it, old files stay readable while `k1` is listed; drop `k1` after all old files are purged (retention period) |
| `BLIND_INDEX_KEY` | do not rotate casually: every phone/PAN lookup hash must be recomputed |
| `DJANGO_SECRET_KEY` | change and restart: signs everyone out |
| Razorpay / WhatsApp / DB / Redis | change at the provider or in `.env.prod`, then `dc up -d` |

A staff member loses their phone: Staff page → Edit → Reset 2FA (ends their sessions).

## 6. Monitoring and health

- External uptime check on `https://<domain>/healthz` (any uptime service), alert after 2 failures.
- Readiness (DB + Redis), internal only: `dc exec web python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/readyz').read())"`
- Logs: `dc logs -f --tail=200 web worker` (JSON, PII-scrubbed, rotated at 10 MB × 5 per service).
  Access logs never contain query strings.
- Errors: Sentry, if `SENTRY_DSN` is set (request bodies, cookies, user data and local variables are stripped).
- Dashboard numbers worth watching: failed requests today, unpaid value, escalated follow-ups.
- Audit chain: Audit log page → Verify integrity (or `dc exec web python manage.py verify_audit_chain`), weekly.

## 7. Common incidents

| Symptom | Check | Fix |
|---|---|---|
| Customer paid, no PDF | Payments page: link status; `dc logs worker | grep confirm_payment` | the 10-minute reconciliation picks it up; force: report request → Retry |
| Webhooks rejected (400) | audit log `webhook.signature_invalid` | secret in `.env.prod` ≠ dashboard secret |
| `payment_mismatch` events | Audit log | amount/currency differs from the link: investigate before refunding/delivering manually |
| Bot silent | `dc logs worker | grep process_whatsapp_event`; Meta webhook delivery log | token expired (`whatsapp_auth` errors) → new System User token |
| Reports stuck "fetching" | report request list filtered by status | self-heals every 15 min; provider outage → check provider status |
| Queue backlog | `dc exec worker celery -A config inspect active` | raise `CELERY_CONCURRENCY`, `dc up -d worker` |
| Disk filling | `docker system df`, `/var/backups/cibil` | prune old images: `docker image prune -a --filter until=720h` |

Data incident (suspected breach): rotate the affected secrets (section 5), preserve logs and the audit log,
and follow the DPDP Act 2023 breach-notification obligations (notify the Data Protection Board and affected
customers); involve legal counsel.
