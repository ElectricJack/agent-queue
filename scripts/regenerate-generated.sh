#!/usr/bin/env bash
# Regenerate every committed generated artifact from this checkout's sources.
#
# Usage:
#   scripts/regenerate-generated.sh            # rewrite every artifact in place
#   scripts/regenerate-generated.sh --check    # exit 1 naming each stale file; writes nothing
#   scripts/regenerate-generated.sh --list     # print the artifact paths, one per line
#
# The artifacts are the paths .gitattributes marks `merge=aq-generated`.  Each
# is a pure function of the source, so none of them is ever hand-merged: when a
# merge or rebase conflicts in one, take either side (`git checkout --ours --
# <path>`), resolve the source files, run this script and commit what it
# writes.  The development publisher does exactly that when it merges a task
# branch (the project's `regenerate` development policy), so a conflict
# confined to these files never parks a candidate.
#
# --check regenerates inside a scratch copy of the working tree (tracked and
# untracked files, ignored ones excluded) and reports every file the
# regeneration changed there, so it sees uncommitted edits and leaves this
# checkout untouched.  tests/test_generated_artifacts.py keeps GENERATED below
# and .gitattributes in step.
#
# Prerequisites are those of the individual generators, above all the pinned
# openapi-python-client and ruff that scripts/regenerate-api-client.sh checks.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Every path the steps below write.  A new generator adds its output here, to
# .gitattributes (merge=aq-generated) and a step to regenerate().
GENERATED=(
    tests/selection_catalogue.json
    docs/reference/cli-command-inventory.json
    docs/reference/configuration-schema.json
    docs/reference/playbook-commands/README.md
    src/playbook_v2_schema.json
    src/tools/command_catalogue.json
    openapi.json
    packages/aq-client
    scripts/aq-client-boilerplate.sha256
)

MODE=""
for arg in "$@"; do
    case "$arg" in
        --check | --list)
            if [[ -n "$MODE" && "$MODE" != "$arg" ]]; then
                echo "Error: pass one of --check and --list." >&2
                exit 2
            fi
            MODE="$arg"
            ;;
        *)
            echo "Error: unknown argument: $arg" >&2
            echo "Usage: $0 [--check | --list]" >&2
            exit 2
            ;;
    esac
done

if [[ "$MODE" == "--list" ]]; then
    printf '%s\n' "${GENERATED[@]}"
    exit 0
fi

# The same interpreter choice as regenerate-api-client.sh: the checkout's own
# venv when it has one, python3 otherwise (some images have no `python`).
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "Error: no Python interpreter found; expected $ROOT_DIR/.venv/bin/python or python3 on PATH." >&2
    exit 1
fi

# Run every generator against the tree at $1.  They are independent, so they
# run concurrently; each one's output is shown only when it fails.
regenerate() {
    local root="$1" logs
    logs="$(mktemp -d)"
    local -a names=() pids=()
    step() {
        local name="$1"
        shift
        (cd "$root" && "$@") >"$logs/$name.log" 2>&1 &
        pids+=("$!")
        names+=("$name")
    }
    step selection-catalogue "$PYTHON" scripts/generate-selection-catalogue.py
    # CLI inventory reads the packaged catalogue, so rebuild it first.
    if ! (cd "$root" && "$PYTHON" scripts/generate-command-catalogue.py); then
        return 1
    fi
    step cli-command-inventory "$PYTHON" scripts/generate-cli-command-inventory.py
    step configuration-schema "$PYTHON" scripts/generate-config-schema-inventory.py
    step playbook-command-docs "$PYTHON" scripts/gen-command-docs.py
    step playbook-schema "$PYTHON" scripts/generate-playbook-schema.py
    # openapi.json, packages/aq-client/ and the boilerplate digests.
    step api-client ./scripts/regenerate-api-client.sh --offline
    local failed=0 i
    for i in "${!pids[@]}"; do
        if ! wait "${pids[$i]}"; then
            echo "Error: ${names[$i]} generation failed:" >&2
            sed 's/^/  /' "$logs/${names[$i]}.log" >&2
            failed=1
        fi
    done
    rm -rf "$logs"
    return "$failed"
}

if [[ -z "$MODE" ]]; then
    regenerate "$ROOT_DIR"
    echo "Regenerated: ${GENERATED[*]}"
    exit 0
fi

# --check: regenerate a scratch copy that is its own Git repository, so
# `git status` there names every file the regeneration changed -- generated
# artifacts and hand-edited pages with generated blocks alike.
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/aq-regenerate-check.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT
COPY="$SCRATCH/tree"
mkdir -p "$COPY"
(
    cd "$ROOT_DIR"
    git ls-files -z --cached --others --exclude-standard \
        | while IFS= read -r -d '' path; do
            # A tracked file deleted in the working tree is not part of it.
            if [[ -e "$path" || -L "$path" ]]; then
                printf '%s\0' "$path"
            fi
        done \
        | xargs -0 --no-run-if-empty cp -P --parents -t "$COPY" --
)
git -C "$COPY" init -q
git -C "$COPY" add -A
git -C "$COPY" -c user.name=check -c user.email=check@localhost \
    commit -q --no-verify -m "working tree before regeneration"

regenerate "$COPY"

STALE="$(git -C "$COPY" status --porcelain --untracked-files=all)"
if [[ -n "$STALE" ]]; then
    echo "Generated files are stale:"
    sed 's/^/  /' <<<"$STALE"
    echo "Run scripts/regenerate-generated.sh and commit what it writes."
    exit 1
fi
echo "Generated files are current: ${GENERATED[*]}"
