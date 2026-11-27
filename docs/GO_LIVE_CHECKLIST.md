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
- [ ] **(blocking)** `CREDIT_PROVIDER=smartauth` with production `BUREAU_B_BASE_URL` and `BUREAU_B_AUTHKEY`; one
      real report fetched, its PDF (befisc_reportforge) checked by eye (layout, score, all pages) and delivered
      end to end. `ALLOW_MOCK_ADAPTERS` is unset. (`surepass` remains available as an alternative.)
- [ ] SmartAuth confirmed: mobile format (10 digits vs 91…), the "no credit history" response (status 1
      without `TrueLinkCreditReport`?) and billing of timed-out calls.
- [ ] befisc_reportforge: a new version is used by changing the pinned commit in
      `backend/requirements/base.txt` after review; never an unpinned `git+…` line.
- [ ] **(blocking)** Surepass has no OTP step: legal sign-off that consent + WhatsApp number + PAN guard is an
      acceptable verification for releasing a credit report, or a provider flow with OTP is used instead.
- [ ] Surepass confirmed: no-hit response format, accepted gender values, billing of timed-out calls,
      prefill "not found" response, and whether prefill uses the same token.
- [ ] **(blocking)** Legal sign-off on prefill-by-mobile: the bureau's details for a number may belong to
      someone other than the WhatsApp user (recycled or shared numbers); masked "Is this you?" confirmation
      plus the PAN guard are the only checks.
- [ ] **(blocking)** Legal sign-off on the alternate mobile number: when nothing is found for the WhatsApp
      number (or the customer says "Not me"), they may send up to two other numbers. Details found for such
      a number are used only after the customer types the matching PAN (one attempt), and the report is
      then requested with that number. Confirm with Surepass that this use of a customer-supplied number is
      allowed, and that the consent wording in `copy.ASK_ALT_MOBILE` is enough.
- [ ] **(blocking)** Razorpay live keys (`rzp_live_…`); webhook registered with the five events; one real ₹1
      payment and refund done end to end.
- [ ] **(blocking)** WhatsApp: verified business, production number, long-lived System User token, webhook
      subscribed to `messages`; templates `payment_link_v1`, `payment_reminder_v1`, `report_delivery_v1`
      (document header) and `report_expired_v1` **approved** (submit with
      `backend/scripts/submit_whatsapp_templates.py --submit`, check with `--status`); one real conversation
      completed.
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
