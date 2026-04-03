#!/usr/bin/env bash
# Regenerate the typed Python API client from the daemon's OpenAPI spec.
#
# Usage:
#   ./scripts/regenerate-api-client.sh              # daemon must be running
#   ./scripts/regenerate-api-client.sh --from-file  # use saved openapi.json
#
# Prerequisites:
#   pip install openapi-python-client
#
# The generated client lives in packages/aq-client/ and should be committed.
# After regenerating, reinstall it:
#   pip install -e packages/aq-client/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SPEC_FILE="$ROOT_DIR/openapi.json"
CLIENT_DIR="$ROOT_DIR/packages/aq-client"
API_URL="${AGENT_QUEUE_API_URL:-http://127.0.0.1:8081}"

if [[ "${1:-}" != "--from-file" ]]; then
    echo "Fetching OpenAPI spec from $API_URL/openapi.json ..."
    curl -sf "$API_URL/openapi.json" > "$SPEC_FILE"
    echo "Saved to $SPEC_FILE"
else
    if [[ ! -f "$SPEC_FILE" ]]; then
        echo "Error: $SPEC_FILE not found. Start the daemon and run without --from-file first." >&2
        exit 1
    fi
    echo "Using saved spec: $SPEC_FILE"
fi

# Count paths in spec
PATHS=$(python3 -c "import json; print(len(json.load(open('$SPEC_FILE'))['paths']))")
echo "Spec has $PATHS paths"

# Remove old client and regenerate
if [[ -d "$CLIENT_DIR" ]]; then
    rm -rf "$CLIENT_DIR"
fi

openapi-python-client generate --path "$SPEC_FILE" --output-path "$CLIENT_DIR"
echo "Generated client at $CLIENT_DIR"

# Reinstall
pip install -e "$CLIENT_DIR" --quiet
echo "Installed agent-queue-api-client"

echo "Done. Don't forget to commit packages/aq-client/ and openapi.json"
