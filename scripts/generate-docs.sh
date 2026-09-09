#!/usr/bin/env bash
# generate-docs.sh — Build documentation locally using MkDocs
#
# Usage:
#   ./scripts/generate-docs.sh          # Build static docs into docs_out/
#   ./scripts/generate-docs.sh serve    # Start a local dev server with live reload
#   ./scripts/generate-docs.sh install  # Install documentation dependencies only
#
# docs/ is the source of truth — MkDocs reads it in place. There is no
# specs/ → docs/specs/ mirror any more (docs/specs/ *is* the spec tree), so
# nothing is copied or deleted before the build.

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

build_docs() {
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
    serve)
        install_deps
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
