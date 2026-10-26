#!/bin/sh
# Installs and configures WordPress + WooCommerce unattended. Idempotent: running it again changes nothing
# that is already right. It stops at the first error (set -e) so a broken setup is loud, not silent.
set -eu

log() { printf '[setup] %s\n' "$*"; }
: "${WP_URL:?}" "${WP_TITLE:?}" "${WP_ADMIN_USER:?}" "${WP_ADMIN_PASSWORD:?}" "${WP_ADMIN_EMAIL:?}"
: "${WP_TIMEZONE:?}" "${WC_CONSUMER_KEY:?}" "${WC_CONSUMER_SECRET:?}"
# wp-config.php reads these from the environment each time WP-CLI runs; if they are missing it falls back to
# host "mysql" and WordPress fails with a misleading "error establishing a database connection". Fail clearly instead.
: "${WORDPRESS_DB_HOST:?WORDPRESS_DB_HOST must be set (the setup container needs the same WORDPRESS_DB_* as the wordpress container)}"
: "${WORDPRESS_DB_NAME:?}" "${WORDPRESS_DB_USER:?}" "${WORDPRESS_DB_PASSWORD:?}"
DB_HOST="${WORDPRESS_DB_HOST%%:*}"

cd /var/www/html

log "waiting for wp-config.php (written by the wordpress container)"
until [ -f wp-config.php ]; do sleep 2; done
log "waiting for the database at ${DB_HOST}:3306"
until nc -z "$DB_HOST" 3306; do sleep 2; done

# --- WordPress ----------------------------------------------------------------------------------------
if wp core is-installed 2>/dev/null; then
  log "WordPress already installed"
else
  log "installing WordPress"
  wp core install --url="$WP_URL" --title="$WP_TITLE" --admin_user="$WP_ADMIN_USER" \
    --admin_password="$WP_ADMIN_PASSWORD" --admin_email="$WP_ADMIN_EMAIL" --skip-email
fi
wp option update timezone_string "$WP_TIMEZONE"
log "must-use plugin: keep generated URLs on the site's own scheme (see keep-url-scheme.php)"
mkdir -p wp-content/mu-plugins
cp /setup/keep-url-scheme.php wp-content/mu-plugins/keep-url-scheme.php
log "pretty permalinks (the REST API lives at /wp-json/...)"
wp rewrite structure '/%postname%/' --hard

# --- WooCommerce --------------------------------------------------------------------------------------
if wp plugin is-installed woocommerce; then
  log "WooCommerce already installed"
else
  set --
  [ -z "${WC_VERSION:-}" ] || set -- --version="$WC_VERSION"
  log "installing WooCommerce ${WC_VERSION:-(latest)}"
  wp plugin install woocommerce "$@"
fi
wp plugin activate woocommerce

log "store settings: India, INR, Cash on delivery, no onboarding wizard"
wp option update woocommerce_currency INR
wp option update woocommerce_default_country "IN:MH"
wp option update woocommerce_onboarding_profile '{"skipped":true}' --format=json
# Hide the admin onboarding task lists. WooCommerce 11 deprecated the old "woocommerce_task_list_hidden" option
# (it intercepts the update, applies it elsewhere and returns the old value, which makes WP-CLI report a
# misleading "Could not update option"), so set the option it now reads: the list of hidden lists.
wp option update woocommerce_task_list_hidden_lists '["setup","extended"]' --format=json
# New WooCommerce installs start in "coming soon" mode: visitors who are not logged in see a placeholder instead of the
# shop. Make the store live (seen on the HP: the admin saw the shop, an incognito window would not have).
wp option update woocommerce_coming_soon no
wp option update woocommerce_cod_settings \
  '{"enabled":"yes","title":"Cash on delivery","description":"Pay with cash when your order is delivered.","instructions":"Pay with cash when your order is delivered.","enable_for_methods":[],"enable_for_virtual":"yes"}' \
  --format=json

# A physical product needs a shipping method or checkout has nothing to offer. Zone 0 is
# "locations not covered by other zones", so free shipping there works for any address.
if wp wc shipping_zone_method list 0 --user="$WP_ADMIN_USER" --field=method_id | grep -qx free_shipping; then
  log "free shipping already set up"
else
  log "adding free shipping"
  wp wc shipping_zone_method create 0 --method_id=free_shipping --user="$WP_ADMIN_USER" >/dev/null
fi

if [ "$(wp wc product list --user="$WP_ADMIN_USER" --format=count)" = "0" ]; then
  log "creating a demo product"
  wp wc product create --name="Demo Mug" --type=simple --regular_price=499 --status=publish \
    --user="$WP_ADMIN_USER" >/dev/null
fi

# --- the API key the MCP server will use (we choose it, the store just learns it) -------------------------
log "registering the WooCommerce REST API key"
wp eval-file /setup/create-api-key.php

log "versions: WordPress $(wp core version), WooCommerce $(wp plugin get woocommerce --field=version)"
log "timezone: $(wp option get timezone_string)"
log "done"
