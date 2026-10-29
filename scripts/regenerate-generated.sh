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
    docs/reference/promotion-flow-schema.json
    docs/reference/playbook-commands/README.md
    src/playbook_v2_schema.json
    src/tools/command_catalogue.json
    openapi.json
    packages/aq-client
    scripts/aq-client-boilerplate.sha256
    tools/bazel/test_suites.json
    tools/bazel/python_data.bzl
    tests/BUILD.bazel
    checks/BUILD.bazel
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

# The same interpreter choice as regenerate-api-client.sh: the checkout's own
# venv when it has one, python3 otherwise (some images have no `python`).
if [[ -n "${AQ_GENERATOR_PYTHON:-}" ]]; then
    PYTHON="$AQ_GENERATOR_PYTHON"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "Error: no Python interpreter found; expected $ROOT_DIR/.venv/bin/python or python3 on PATH." >&2
    exit 1
fi

# Source recordings and module shards own policy; their projections join this registry.
RECORDED_PATHS="$("$PYTHON" "$ROOT_DIR/scripts/generate-recorded-artifacts.py" --list)"
mapfile -t RECORDED <<< "$RECORDED_PATHS"
GENERATED+=("${RECORDED[@]}")

# scripts/bazel/generate_targets.py reuses the committed manifest's collection
# when no test/fixture byte has changed (the fast path: no pytest collection,
# just re-rendering).  It only runs the full `inventory.py collect` -- timed
# at ~2 minutes wall-clock (39421 tests; see the S5 close summary) on this
# checkout -- when that comparison finds drift, which is the one error this
# wraps and retries with a freshly collected manifest.
bazel_generate() {
    local python="$1" root="$2"
    shift 2
    local log status collection
    if log="$(cd "$root" && "$python" scripts/bazel/generate_targets.py "$@" 2>&1)"; then
        printf '%s\n' "$log"
        return 0
    fi
    status=$?
    if ! grep -q "test/fixture source drift" <<<"$log"; then
        printf '%s\n' "$log" >&2
        return "$status"
    fi
    echo "Bazel manifest collection is stale; running a full inventory.py collect (~2 minutes) to refresh it..." >&2
    collection="$(mktemp "${TMPDIR:-/tmp}/aq-bazel-collection.XXXXXX.json")"
    if ! (cd "$root" && "$python" scripts/bazel/inventory.py collect --output "$collection"); then
        rm -f "$collection"
        return 1
    fi
    (cd "$root" && "$python" scripts/bazel/generate_targets.py "$@" --collection "$collection")
    status=$?
    rm -f "$collection"
    return "$status"
}

# Every generated BUILD.bazel file: enumerated from the committed tree via
# `git ls-files`, not by invoking generate_targets.py, whose definition-level
# analysis over every selected test module (scripts/bazel/test_dependencies.py's
# DependencyAnalyzer.discover) takes minutes even on its fast, no-recollection
# path. The committed set is authoritative between runs; the bazel-targets
# step below is the only place that cost is paid, once, to actually rewrite
# fresh content into these same paths.
mapfile -t BAZEL_TARGETS < <(cd "$ROOT_DIR" && git ls-files -- BUILD.bazel '**/BUILD.bazel')
GENERATED+=("${BAZEL_TARGETS[@]}")
if [[ "$MODE" == "--list" ]]; then
    printf '%s\n' "${GENERATED[@]}"
    exit 0
fi

# Check the original Git history/index before --check creates a fresh scratch
# commit, and before any generator can overwrite reviewed legacy evidence.
"$PYTHON" "$ROOT_DIR/src/integration/generated_merge_driver.py" --check-worktree "$ROOT_DIR"

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
    # generate_targets.py reads tests/selection_catalogue.json as its first
    # statement, so this must be settled before the bazel-targets step starts.
    if ! (cd "$root" && "$PYTHON" scripts/generate-selection-catalogue.py); then
        return 1
    fi
    step recorded-artifacts "$PYTHON" scripts/generate-recorded-artifacts.py
    # These profiles have authored prose/config and generated capability blocks.
    step worker-capabilities "$PYTHON" scripts/generate-worker-capabilities.py
    # CLI inventory reads the packaged catalogue, so rebuild it first.
    if ! (cd "$root" && "$PYTHON" scripts/generate-command-catalogue.py); then
        return 1
    fi
    step cli-command-inventory "$PYTHON" scripts/generate-cli-command-inventory.py
    step configuration-schema "$PYTHON" scripts/generate-config-schema-inventory.py
    step promotion-flow-schema "$PYTHON" scripts/generate-promotion-flow-schema.py
    step playbook-command-docs "$PYTHON" scripts/gen-command-docs.py
    step playbook-schema "$PYTHON" scripts/generate-playbook-schema.py
    # openapi.json, packages/aq-client/ and the boilerplate digests.
    step api-client ./scripts/regenerate-api-client.sh --offline
    # Every BUILD.bazel/BUILD file, tools/bazel/test_suites.json and
    # tools/bazel/python_data.bzl; see bazel_generate() above for the drift fallback.
    step bazel-targets bazel_generate "$PYTHON" "$root"
    local failed=0 i
    for i in "${!pids[@]}"; do
        if ! wait "${pids[$i]}"; then
            echo "Error: ${names[$i]} generation failed:" >&2
            sed 's/^/  /' "$logs/${names[$i]}.log" >&2
            failed=1
        fi
    done
    # The Bazel manifest and suite files hash test sources that the generators
    # above may rewrite, so they run last.  The manifest retains its collection
    # evidence: recollect only when a test or fixture changed since it was taken.
    if [[ "$failed" == 0 ]]; then
        local gen=scripts/bazel/generate_targets.py
        if ! (cd "$root" && "$PYTHON" "$gen" >"$logs/bazel.log" 2>&1); then
            if ! (cd "$root" \
                && "$PYTHON" scripts/bazel/inventory.py collect --output "$logs/collection.json" \
                && "$PYTHON" "$gen" --collection "$logs/collection.json") >"$logs/bazel.log" 2>&1; then
                echo "Error: bazel target generation failed:" >&2
                sed 's/^/  /' "$logs/bazel.log" >&2
                failed=1
            fi
        fi
    fi
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
