#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SPEC_FILE="$ROOT_DIR/openapi.json"
OUTPUT_DIR="$ROOT_DIR/packages/aq-ts-client/src"

if [[ "$1" == "--from-file" ]] 2>/dev/null; then
    echo "Using saved spec at $SPEC_FILE"
else
    echo "Fetching OpenAPI spec from running daemon..."
    curl -sf http://127.0.0.1:8081/openapi.json > "$SPEC_FILE" \
        || { echo "Failed to fetch spec — is the daemon running? Use --from-file to use saved spec."; exit 1; }
fi

echo "Generating TypeScript client..."
npx -w packages/aq-ts-client @hey-api/openapi-ts \
    --input "$SPEC_FILE" \
    --output "$OUTPUT_DIR" \
    --client @hey-api/client-fetch

echo "Done — generated client at $OUTPUT_DIR"
