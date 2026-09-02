#!/usr/bin/env bash
# Regenerate the dashboard's TypeScript client from the daemon's OpenAPI spec.
#
# Usage:
#   ./scripts/regenerate-ts-client.sh --from-file  # use the committed openapi.json
#   ./scripts/regenerate-ts-client.sh --offline    # rebuild the spec in-process
#   ./scripts/regenerate-ts-client.sh              # fetch it from a running daemon
#
# --from-file is the canonical path: run regenerate-api-client.sh --offline
# first, then generate the TS client from the openapi.json it just wrote.
#
# The output tree (packages/aq-ts-client/src/) is gitignored and generated
# on demand; only the committed openapi.json it reads needs to stay current.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SPEC_FILE="$ROOT_DIR/openapi.json"
OUTPUT_DIR="$ROOT_DIR/packages/aq-ts-client/src"

case "${1:-}" in
    --from-file)
        echo "Using saved spec at $SPEC_FILE"
        ;;
    --offline)
        # See src/api/spec.py — the spec needs no daemon, only the checkout.
        echo "Building OpenAPI spec offline from this checkout ..."
        (cd "$ROOT_DIR" && python -m src.api.spec "$SPEC_FILE")
        ;;
    *)
        # Rendered through src.api.spec (not written raw) so the committed
        # openapi.json keeps the indented, diffable format the drift guard in
        # tests/test_api_client_contract.py pins.  See regenerate-api-client.sh.
        echo "Fetching OpenAPI spec from running daemon..."
        curl -sf http://127.0.0.1:8081/openapi.json \
            | (cd "$ROOT_DIR" && python3 -m src.api.spec --stdin "$SPEC_FILE") \
            || { echo "Failed to fetch spec — is the daemon running? Use --offline to build it from this checkout, or --from-file to use the saved spec."; exit 1; }
        ;;
esac

echo "Generating TypeScript client..."
npx -w packages/aq-ts-client @hey-api/openapi-ts \
    --input "$SPEC_FILE" \
    --output "$OUTPUT_DIR" \
    --client @hey-api/client-fetch

echo "Done — generated client at $OUTPUT_DIR"
