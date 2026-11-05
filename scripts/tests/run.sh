#!/bin/sh
# Runs the script tests with the backend's Python environment: it provides cryptography and PyYAML, and the gateway
# package itself, whose secrets-file and seed loaders check what the Cloud Run scripts produce.
# Needs uv; no cluster, Docker or k3d. Usage:  scripts/tests/run.sh [pytest args]
set -eu
cd "$(dirname "$0")/../../backend"
exec uv run python -m pytest ../scripts/tests -p no:cacheprovider -q "$@"
