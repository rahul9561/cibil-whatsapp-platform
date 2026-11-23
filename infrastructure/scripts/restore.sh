#!/usr/bin/env bash
# Restore a database backup (destructive). Run from the repo root:
#   infrastructure/scripts/restore.sh /var/backups/cibil/db-20261123T021500Z.sql.gz.enc --yes
# Report files: decrypt reports-*.tar.gz.enc and untar into the cibil_private_reports volume (see RUNBOOK).
set -euo pipefail

FILE="${1:?usage: restore.sh <db-backup.sql.gz.enc> --yes}"
[ "${2:-}" = "--yes" ] || { echo "This REPLACES the production database. Re-run with --yes to confirm." >&2; exit 1; }
PASS_FILE="${BACKUP_PASSPHRASE_FILE:-/etc/cibil/backup.pass}"
# Override for a different stack, e.g. CIBIL_COMPOSE="docker compose -f a.yml -f b.yml --env-file x"
read -r -a COMPOSE <<< "${CIBIL_COMPOSE:-docker compose -f infrastructure/compose/docker-compose.prod.yml --env-file .env.prod}"

echo "Stopping application containers (database stays up)..."
"${COMPOSE[@]}" stop nginx web worker beat
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "file:$PASS_FILE" -in "$FILE" | gunzip \
  | "${COMPOSE[@]}" exec -T mysql sh -c 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'
echo "Restored. Verifying the audit chain before reopening..."
"${COMPOSE[@]}" run --rm web python manage.py verify_audit_chain
"${COMPOSE[@]}" up -d
echo "Done."
