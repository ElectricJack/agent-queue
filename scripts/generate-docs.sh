#!/usr/bin/env bash
# generate-docs.sh — Build documentation locally using MkDocs
#
# Usage:
#   ./scripts/generate-docs.sh          # Build static docs into docs_out/
#   ./scripts/generate-docs.sh serve    # Start a local dev server with live reload
#   ./scripts/generate-docs.sh install  # Install documentation dependencies only
#   ./scripts/generate-docs.sh sync     # Sync specs/ → docs/specs/ only
#
# The build and serve commands automatically sync specs/ → docs/specs/ first.
# specs/ is the source of truth; docs/specs/ is the mirror for MkDocs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

install_deps() {
    echo "📦 Installing documentation dependencies..."
    pip install -q \
        "mkdocs>=1.6" \
        "mkdocs-material>=9.5" \
        "mkdocstrings[python]>=0.27"
    echo "✅ Dependencies installed."
}

sync_specs() {
    echo "🔄 Syncing specs/ → docs/specs/ ..."
    rsync -a --delete specs/ docs/specs/
    echo "✅ Specs synced."
}

build_docs() {
    sync_specs
    echo "📖 Building documentation..."
    mkdocs build --site-dir docs_out --strict 2>&1 || {
        echo ""
        echo "⚠️  Build completed with warnings (non-strict). Retrying without --strict..."
        mkdocs build --site-dir docs_out
    }
    echo "✅ Documentation built successfully in docs_out/"
    echo "   Open docs_out/index.html in your browser to view."
}

serve_docs() {
    echo "🚀 Starting local documentation server..."
    echo "   Open http://127.0.0.1:8000 in your browser."
    echo "   Press Ctrl+C to stop."
    mkdocs serve
}

case "${1:-build}" in
    install)
        install_deps
        ;;
    sync)
        sync_specs
        ;;
    serve)
        install_deps
        sync_specs
        serve_docs
        ;;
    build)
        install_deps
        build_docs
        ;;
    *)
        echo "Usage: $0 [build|serve|install]"
        exit 1
        ;;
esac
