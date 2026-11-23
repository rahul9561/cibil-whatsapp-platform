#!/usr/bin/env bash
# First-time TLS certificate for the production domain (run once on the VPS, as root, from the repo root).
#   sudo infrastructure/scripts/init-letsencrypt.sh reports.example.com ops@example.com
# Requires: certbot installed on the host (apt install certbot), DNS A/AAAA records pointing here, port 80 open.
# By running this you accept the Let's Encrypt Subscriber Agreement for the given e-mail address.
set -euo pipefail

DOMAIN="${1:?usage: init-letsencrypt.sh <domain> <email>}"
EMAIL="${2:?usage: init-letsencrypt.sh <domain> <email>}"
COMPOSE=(docker compose -f infrastructure/compose/docker-compose.prod.yml --env-file .env.prod)
LIVE="/etc/letsencrypt/live/$DOMAIN"

mkdir -p /var/www/certbot

if [ ! -f "$LIVE/fullchain.pem" ]; then
  echo "Creating a temporary self-signed certificate so nginx can start..."
  mkdir -p "$LIVE"
  openssl req -x509 -nodes -newkey rsa:2048 -days 1 -subj "/CN=$DOMAIN" \
    -keyout "$LIVE/privkey.pem" -out "$LIVE/fullchain.pem" >/dev/null 2>&1
  TEMP_CERT=1
fi

"${COMPOSE[@]}" up -d nginx

if [ "${TEMP_CERT:-0}" = "1" ]; then
  rm -rf "$LIVE"  # certbot refuses to write into a directory it didn't create
fi

certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" \
  --email "$EMAIL" --agree-tos --no-eff-email --non-interactive --keep-until-expiring

# Renewals run from certbot's own systemd timer; reload nginx after each one.
HOOK=/etc/letsencrypt/renewal-hooks/deploy/reload-cibil-nginx.sh
cat > "$HOOK" <<HOOK
#!/bin/sh
cd "$(pwd)" && ${COMPOSE[*]} exec -T nginx nginx -s reload
HOOK
chmod +x "$HOOK"

"${COMPOSE[@]}" exec -T nginx nginx -s reload
echo "TLS ready for https://$DOMAIN (renewal hook: $HOOK)"
