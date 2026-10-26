# A real WooCommerce store (Docker Compose)

Purpose: run a **real** WordPress + WooCommerce so I could check what the fake store could not (see `woo-mcp/tests/contract/`). Later, the Helm chart reuses `store/setup-store.sh` unchanged.

Docker with the compose plugin is needed on the machine that runs the store (I used an Ubuntu laptop). The machine where the code is edited does not need it.

## 1. On the HP

Copy this folder over (from the Mac), then run it:

```bash
# on the Mac, from the project root (replace user and address)
scp -r deploy/compose <user>@<hp-ip>:~/wc-store

# on the HP
cd ~/wc-store
./make-env.sh http://<hp-ip>:8080        # use the HP's LAN address, the one the browser will use
docker compose up -d
docker compose logs -f setup             # wait for the line:  [setup] done
```

- Find the HP's address with `ip -4 a` (a line like `inet 192.168.1.50/24`).
- If `ufw` is on: `sudo ufw allow 8080/tcp`.
- WordPress redirects any other host name to the URL given to `make-env.sh`, so always use exactly that address.
- First start takes a few minutes (it downloads WooCommerce). If `setup` fails, the log says where; it is safe to run again with `docker compose run --rm setup`.

`.env` holds the passwords and the WooCommerce key/secret. It is created with permissions 600 and is ignored by git. Never commit it or paste it into chat.

## 2. Check it by hand

1. Open `http://<hp-ip>:8080` and **add the Demo Mug to the cart and check out with Cash on delivery**.
2. Open `http://<hp-ip>:8080/wp-admin` (user `admin`, password = `WP_ADMIN_PASSWORD` in `.env`) and confirm the order appears under WooCommerce > Orders.

If checkout offers no shipping option or no payment method, a setup step did not apply: check `docker compose logs setup`.

## 3. Run the contract tests (on the Mac)

```bash
cd woo-mcp
export WOO_CONTRACT_URL=http://<hp-ip>:8080           # exactly the WP_URL from .env
export WOO_CONTRACT_KEY=...                           # WC_CONSUMER_KEY from the HP's .env
export WOO_CONTRACT_SECRET=...                        # WC_CONSUMER_SECRET from the HP's .env
uv run pytest tests/contract -v -s
```

Read the `[finding]` lines printed with `-s`: they report what the real store did for the questions I could not answer before:

| Test | Question it settles |
|---|---|
| `test_basic_auth_over_http_...`, `test_what_happens_without_...` | Does key auth over plain HTTP work with `X-Forwarded-Proto`? Is the header really needed? |
| `test_a_different_host_header_...` | Does WordPress redirect REST calls made by an in-cluster host name? |
| `test_the_stores_time_zone_is_ist` | Did the unattended setup apply Asia/Kolkata? |
| `test_order_date_filters_treat_...` | Does the store read my UTC `after`/`before` instants as UTC? |
| `test_a_created_product_is_visible_on_the_storefront` | Does a created product appear on the storefront? |

The tests create products and orders and delete them afterwards.

## Verified against the real store (2026-10-22: all 18 contract tests pass)

| Question | Answer |
|---|---|
| Key auth over plain HTTP | Works, but **only with `X-Forwarded-Proto: https`**: without it the store answers 401. The chart must keep WordPress trusting that header. |
| In-cluster host name | A different `Host` header gets HTTP 200, no redirect. The MCP server can use the service name. |
| Store time zone | `Asia/Kolkata` applied by the setup script. |
| Order `after`/`before` filters | Reads my UTC instants as UTC (checked with a +/-1 minute window). |
| Created product on the storefront | Visible, once generated links keep the configured scheme (`keep-url-scheme.php`). |
| Bad price `"abc"` | The store accepts the request and silently stores an empty price, so my tool validates before sending. |
| Unknown order id | The store answers 400 "ID is invalid."; the client maps it to `not_found`. |
| Customer data | Real orders carry it; my tools strip it. |

## Reset

```bash
docker compose down -v      # deletes the database and WordPress files
rm .env                     # only for new passwords
```

## What the setup script does (store/setup-store.sh)
WordPress core install; time zone Asia/Kolkata; pretty permalinks; WooCommerce install and activation (version `WC_VERSION`, empty = latest, printed at the end so I could pin it); INR, India, Cash on delivery, no onboarding wizard; free shipping in the catch-all zone; a Demo Mug; and registration of the REST API key I chose (`create-api-key.php`). Every step is safe to repeat.

## Known limits of this check
- Image tags follow a major version (`wordpress:7-apache`, `mariadb:11`) so the first run is easy. WordPress must be 7.x because WooCommerce 11.x requires WordPress 7.0+. The Helm chart pins exact versions.
- Runs as a plain Docker stack, not in Kubernetes. It proves the store and my MCP code, not NetworkPolicies, Ingress or probes.
- The WordPress log no longer shows the WooCommerce consumer key: `store/hide-auth-user.conf` (mounted by `docker-compose.yml`, same file as the Helm chart) logs `-` instead of the Basic-auth user name. Restart the stack (`docker compose up -d`) to apply it. See `docs/threat-model.md`.
