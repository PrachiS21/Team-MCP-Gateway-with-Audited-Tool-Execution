# Shared settings and helpers for the Cloud Run scripts (gcp-*.sh), sourced after lib.sh.
# Settings (override with environment variables):
#   GCP_PROJECT  project id                    (default: gcloud's current project)
#   GCP_REGION   region for everything         (default us-central1: Google quotes the free tiers for it, and the
#                                               Cloud Storage free tier covers US regions only)
#   GCP_REPO     Artifact Registry repository  (default mcp)

GCP_REGION="${GCP_REGION:-us-central1}"
GCP_REPO="${GCP_REPO:-mcp}"

# The Secret Manager secrets (gcp-secrets.sh makes them). Five values: Secret Manager is free up to six.
GCP_SECRETS="gateway-env woo-mcp-token gst-mcp-token woo-consumer-key woo-consumer-secret"

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
