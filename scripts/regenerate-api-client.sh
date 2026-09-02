#!/usr/bin/env bash
# Regenerate the typed Python API client from the daemon's OpenAPI spec.
#
# Usage:
#   ./scripts/regenerate-api-client.sh --offline    # build the spec in-process
#   ./scripts/regenerate-api-client.sh              # fetch it from a running daemon
#   ./scripts/regenerate-api-client.sh --from-file  # use saved openapi.json
#
# --offline is the canonical path: the spec is a pure function of the
# checkout, so it needs no daemon and cannot pick up another instance's state.
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

case "${1:-}" in
    --offline)
        # No daemon needed: create_app() builds the whole route surface from
        # the command registry, so the spec is a pure function of the
        # checkout.  This is what the drift guard in
        # tests/test_api_client_contract.py compares against.
        echo "Building OpenAPI spec offline from this checkout ..."
        (cd "$ROOT_DIR" && python -m src.api.spec "$SPEC_FILE")
        ;;
    --from-file)
        if [[ ! -f "$SPEC_FILE" ]]; then
            echo "Error: $SPEC_FILE not found. Run with --offline first." >&2
            exit 1
        fi
        echo "Using saved spec: $SPEC_FILE"
        ;;
    *)
        # The daemon serves the spec minified.  Writing that straight to
        # openapi.json produces a single-line, undiffable file that the drift
        # guard in tests/test_api_client_contract.py rejects, so render it
        # through the same src.api.spec writer --offline uses.  json parsing
        # happens before anything is written, so a failed fetch (pipefail is
        # on) leaves the existing openapi.json untouched.
        echo "Fetching OpenAPI spec from $API_URL/openapi.json ..."
        curl -sf "$API_URL/openapi.json" \
            | (cd "$ROOT_DIR" && python3 -m src.api.spec --stdin "$SPEC_FILE")
        ;;
esac

# Count paths in spec
PATHS=$(python3 -c "import json; print(len(json.load(open('$SPEC_FILE'))['paths']))")
echo "Spec has $PATHS paths"

# Remove old client and regenerate
if [[ -d "$CLIENT_DIR" ]]; then
    rm -rf "$CLIENT_DIR"
fi

# --config pins the package name: it would otherwise be derived from the
# spec's info.title ("Agent Q API" -> agent_q_api_client), renaming the
# package that src/cli/client.py imports by name.
openapi-python-client generate \
    --path "$SPEC_FILE" \
    --output-path "$CLIENT_DIR" \
    --config "$SCRIPT_DIR/openapi-python-client.yaml"
echo "Generated client at $CLIENT_DIR"

# Reinstall.  PEP 668 marks some interpreters externally managed; the client
# is a dev artifact, so fall back rather than failing the regeneration.
pip install -e "$CLIENT_DIR" --quiet \
    || pip install -e "$CLIENT_DIR" --quiet --break-system-packages \
    || echo "WARNING: could not pip install $CLIENT_DIR — install it manually" >&2
echo "Installed agent-queue-api-client"

echo "Done. Don't forget to commit packages/aq-client/ and openapi.json"
