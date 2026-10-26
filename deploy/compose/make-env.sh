#!/bin/sh
# Creates .env with random passwords and the WooCommerce API key/secret we will give the MCP server.
# Usage:  ./make-env.sh http://<this-machine-ip>:8080
# The URL must be the one you will open in a browser AND the one the contract test uses: WordPress
# redirects any other host name to it.
set -eu
[ $# -eq 1 ] || { echo "usage: $0 http://<this-machine-ip>:8080" >&2; exit 2; }
[ ! -e .env ] || { echo ".env already exists; delete it first if you want new passwords (and run: docker compose down -v)" >&2; exit 1; }
URL="$1"
PORT=$(printf '%s' "$URL" | sed -n 's#.*:\([0-9][0-9]*\)$#\1#p'); PORT=${PORT:-8080}
rand() { openssl rand -hex "$1"; }
umask 077
cat > .env <<ENV
WP_URL=$URL
WP_PORT=$PORT
WP_TITLE=WooCommerce Store
WP_ADMIN_USER=admin
WP_ADMIN_PASSWORD=$(rand 12)
WP_ADMIN_EMAIL=admin@example.com
WP_TIMEZONE=Asia/Kolkata
WC_VERSION=
DB_NAME=wordpress
DB_USER=wordpress
DB_PASSWORD=$(rand 16)
DB_ROOT_PASSWORD=$(rand 16)
WC_CONSUMER_KEY=ck_$(rand 20)
WC_CONSUMER_SECRET=cs_$(rand 20)
ENV
echo "Wrote .env (permissions 600). Next:  docker compose up -d   then   docker compose logs -f setup"
echo "Store: $URL    Admin: $URL/wp-admin  (user admin; password is WP_ADMIN_PASSWORD in .env)"
