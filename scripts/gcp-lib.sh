# Shared settings and helpers for the Cloud Run scripts (gcp-*.sh), sourced after lib.sh.
# Settings (override with environment variables):
#   GCP_PROJECT  project id                    (default: gcloud's current project)
#   GCP_REGION   region for everything         (default us-central1: Google quotes the free tiers for it, and the
#                                               Cloud Storage free tier covers US regions only)
#   GCP_REPO     Artifact Registry repository  (default mcp)
#   WOO_MCP_URL  woo-mcp's MCP endpoint when it runs outside Cloud Run, e.g. on the HP next to the store, published
#                with Tailscale Funnel: https://woo-mcp.<tailnet>.ts.net/mcp. Unset: woo-mcp runs on Cloud Run.

GCP_REGION="${GCP_REGION:-us-central1}"
GCP_REPO="${GCP_REPO:-mcp}"
WOO_MCP_URL="${WOO_MCP_URL:-}"

# The Secret Manager secrets (gcp-secrets.sh makes them). Secret Manager is free up to six values.
GCP_SECRETS="gateway-env woo-mcp-token gst-mcp-token"
GCP_STORE_SECRETS="woo-consumer-key woo-consumer-secret"  # only for a woo-mcp on Cloud Run

gcp_project() {
  p="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
  [ -n "$p" ] || die "no project: set GCP_PROJECT=<project id>, or run: gcloud config set project <project id>"
  case "$p" in *[!a-z0-9-]*) die "GCP_PROJECT '$p' is not a project id (lowercase letters, digits, hyphens)" ;; esac
  printf '%s' "$p"
}

secret_exists() { gcloud secrets describe "$1" --project="$PROJECT" >/dev/null 2>&1; }

# valid <setting> <value> <extended regex>: stops unless the value is one line matching the whole pattern
valid() {
  case "$2" in *'
'*) die "$1 must be a single line" ;; esac
  printf '%s\n' "$2" | grep -Eqx "$3" || die "$1 '$2' is not valid"
}
