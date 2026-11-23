#!/usr/bin/env bash
# Encrypted backups of the database and the private report files. Run from the repo root, e.g. via cron:
#   15 2 * * * cd /opt/cibil && infrastructure/scripts/backup.sh >> /var/log/cibil-backup.log 2>&1
#
# BACKUP_PASSPHRASE_FILE  file holding the encryption passphrase (chmod 600; keep a copy OFF the server)
# BACKUP_DIR              where encrypted archives go (default /var/backups/cibil)
# BACKUP_RETENTION_DAYS   local retention (default 14)
# BACKUP_RCLONE_REMOTE    optional rclone destination for an off-site copy, e.g. "s3:my-bucket/cibil"
#
# NOTE: these archives do NOT contain the application keys (.env.prod). Without FIELD_ENCRYPTION_KEYS,
# BLIND_INDEX_KEY and REPORT_FILE_ENCRYPTION_KEYS the restored data is unreadable: store .env.prod
# separately in a password manager / secrets vault.
set -euo pipefail
umask 077

PASS_FILE="${BACKUP_PASSPHRASE_FILE:-/etc/cibil/backup.pass}"
DIR="${BACKUP_DIR:-/var/backups/cibil}"
KEEP="${BACKUP_RETENTION_DAYS:-14}"
# Override for a different stack, e.g. CIBIL_COMPOSE="docker compose -f a.yml -f b.yml --env-file x"
read -r -a COMPOSE <<< "${CIBIL_COMPOSE:-docker compose -f infrastructure/compose/docker-compose.prod.yml --env-file .env.prod}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

[ -r "$PASS_FILE" ] || { echo "passphrase file $PASS_FILE not readable" >&2; exit 1; }
mkdir -p "$DIR"
encrypt() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass "file:$PASS_FILE"; }

echo "[$TS] database..."
"${COMPOSE[@]}" exec -T mysql sh -c \
  'exec mysqldump --single-transaction --quick --routines --triggers -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' \
  | gzip -9 | encrypt > "$DIR/db-$TS.sql.gz.enc"

echo "[$TS] report files..."
docker run --rm -v "${CIBIL_REPORTS_VOLUME:-cibil_private_reports}":/data:ro alpine:3.20 tar czf - -C /data . \
  | encrypt > "$DIR/reports-$TS.tar.gz.enc"

# A backup that can't be decrypted is not a backup: check both archives now.
for f in "$DIR/db-$TS.sql.gz.enc" "$DIR/reports-$TS.tar.gz.enc"; do
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "file:$PASS_FILE" -in "$f" | gzip -t
done

find "$DIR" -name '*.enc' -mtime +"$KEEP" -delete
if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
  rclone copy "$DIR" "$BACKUP_RCLONE_REMOTE" --include "*-$TS.*"
fi
echo "[$TS] done: $(ls -1 "$DIR" | grep -c "$TS") archives written and verified"
