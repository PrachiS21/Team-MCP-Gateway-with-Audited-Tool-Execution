#!/bin/sh
# Runs the chart tests with the backend's Python environment (it provides the gateway code the tests import).
# Needs `helm` and `uv`; no cluster. Usage:  deploy/helm/tests/run.sh [pytest args]
set -eu
cd "$(dirname "$0")/../../../backend"
exec uv run --with pyyaml python -m pytest ../deploy/helm/tests -p no:cacheprovider -q "$@"
