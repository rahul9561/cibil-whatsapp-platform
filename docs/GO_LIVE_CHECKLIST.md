# Go-live checklist

Tick every item before real customers are let in. Items marked **(blocking)** are hard requirements.

## Legal and business
- [ ] **(blocking)** Signed agreement with an authorised credit-information partner; it permits charging the
      customer for their report and allows WhatsApp delivery (ARCHITECTURE §14).
- [ ] **(blocking)** Consent text (`apps/conversations/copy.py`) reviewed by counsel; bump `CONSENT_VERSION` if changed.
- [ ] Privacy notice and refund policy published; linked from the payment description / bot messages.
- [ ] Report price, validity and PDF retention confirmed (`REPORT_PRICE_PAISE`, `REPORT_VALIDITY_DAYS`,
      `REPORT_PDF_RETENTION_DAYS`).
- [ ] DPDP Act 2023: data-retention schedule, erasure procedure and breach-response contact agreed.

## Integrations
- [ ] **(blocking)** `CREDIT_PROVIDER=surepass` with a production `SUREPASS_API_TOKEN`; one real report fetched
      and saved end to end. `ALLOW_MOCK_ADAPTERS` is unset.
- [ ] **(blocking)** Surepass has no OTP step: legal sign-off that consent + WhatsApp number + PAN guard is an
      acceptable verification for releasing a credit report, or a provider flow with OTP is used instead.
- [ ] Surepass confirmed: no-hit response format, accepted gender values, billing of timed-out calls.
- [ ] **(blocking)** Razorpay live keys (`rzp_live_…`); webhook registered with the five events; one real ₹1
      payment and refund done end to end.
- [ ] **(blocking)** WhatsApp: verified business, production number, long-lived System User token, webhook
      subscribed to `messages`; templates `payment_link_v1`, `payment_reminder_v1`, `report_delivery_v1`
      (document header) **approved**; one real conversation completed.
- [ ] Re-checked against current vendor docs: Razorpay duplicate-`reference_id` error and link-list filter;
      WhatsApp Graph API version and error codes.

## Infrastructure and security
- [ ] Server hardened (SSH keys only, ufw, unattended upgrades); Docker and images up to date.
- [ ] TLS certificate issued; `https://<domain>` scores A on an SSL test; HTTP redirects to HTTPS.
- [ ] `.env.prod` has fresh secrets, `chmod 600`, and a copy in the password manager.
- [ ] `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` set to the domain only.
- [ ] `dc exec web python manage.py check --deploy` shows no errors (HSTS preload warning is a deliberate choice).
- [ ] `pip-audit -r backend/requirements/prod.txt` and `npm audit` (frontend) report no known vulnerabilities.
- [ ] Super Admin, Admin and Finance accounts have 2FA enrolled; no shared accounts; dev seed accounts absent.

## Operations
- [ ] Nightly backup cron installed; backup passphrase stored off-server; **one restore tested**.
- [ ] Uptime monitoring on `/healthz` with alerts to a person who will act.
- [ ] Sentry (or equivalent) receiving errors, and an owner reads them.
- [ ] Staff trained on the follow-up queue, refunds (four-eyes rule) and PII reveal (reasons are audited).
- [ ] Rollback rehearsed once (deploy previous tag).
