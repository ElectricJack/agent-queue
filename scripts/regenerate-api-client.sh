#!/usr/bin/env bash
# Regenerate the typed Python API client from the daemon's OpenAPI spec.
#
# Usage:
#   ./scripts/regenerate-api-client.sh --offline    # build the spec in-process
#   ./scripts/regenerate-api-client.sh              # fetch it from a running daemon
#   ./scripts/regenerate-api-client.sh --from-file  # use saved openapi.json
#   ... --install                                   # also pip install -e the client
#
# --offline is the canonical path: the spec is a pure function of the
# checkout, so it needs no daemon and cannot pick up another instance's state.
#
# The script writes tracked files and nothing else.  It used to end with an
# unconditional `pip install -e packages/aq-client`, and from a worktree slot
# `pip` is the *shared* venv's: that one line re-pointed
# site-packages/agent_queue_api_client.pth at the slot, after which the daemon,
# the CLI and every other slot's tests imported the client from a tree that is
# reset onto another branch between tasks.  An editable install is a pointer at
# the source directory, so regenerating in place already changes what is
# imported -- the reinstall only matters on a box that never installed the
# client.  It is therefore opt-in (--install), refused in a worker session
# (AQ_DB_SCOPE=worker), and refuses to move an install that currently resolves
# to another tree; both refusals happen before anything is regenerated.  A
# plain run reports where the installed client comes from instead.
#
# Prerequisites:
#   pip install 'openapi-python-client==0.29.0'
#
# The generator version is pinned below and checked before anything is
# generated.  The whole client tree -- including the boilerplate README.md the
# generator writes -- is a function of the generator version, so an unpinned
# generator makes regeneration non-idempotent: a box with a different version
# rewrites files nobody touched and the `git diff --exit-code` idempotence
# check in the child plan's verification section fails for a reason unrelated
# to the spec.  Bump GENERATOR_VERSION and regenerate in the same commit.
#
# The generator version is not the only input: it runs post-generation hooks,
# and the ambient `ruff` they invoke is the second one.  Those hooks are
# declared explicitly in scripts/openapi-python-client.yaml and scoped to the
# Python package, which keeps README.md out of the formatter's reach (recent
# ruff reformats Python code blocks inside Markdown; older ruff, and a box
# with no ruff at all, do not) -- see the note there.  The generated Python is
# still formatted by whatever ruff is installed, so ruff is required below
# rather than silently skipped.
#
# The pin is only a declaration; what checks it against the committed tree is
# tests/test_api_client_contract.py::
# test_generated_client_boilerplate_matches_what_the_pinned_generator_writes,
# which needs the generator installed.  Where it is not, the digests this
# script records in scripts/aq-client-boilerplate.sha256 check the same files
# from the checkout alone.
#
# The generated client lives in packages/aq-client/ and should be committed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SPEC_FILE="$ROOT_DIR/openapi.json"
CLIENT_DIR="$ROOT_DIR/packages/aq-client"
API_URL="${AGENT_QUEUE_API_URL:-http://127.0.0.1:8081}"

MODE=""
INSTALL=0
for arg in "$@"; do
    case "$arg" in
        --offline | --from-file)
            if [[ -n "$MODE" && "$MODE" != "$arg" ]]; then
                echo "Error: $MODE and $arg are different spec sources; pass one." >&2
                exit 2
            fi
            MODE="$arg"
            ;;
        --install)
            INSTALL=1
            ;;
        *)
            # An unrecognised argument used to fall through to the
            # fetch-from-a-daemon branch below, so a typo regenerated the
            # client from whatever daemon happened to be listening.
            echo "Error: unknown argument: $arg" >&2
            echo "Usage: $0 [--offline | --from-file] [--install]" >&2
            exit 2
            ;;
    esac
done

# Where `agent_queue_api_client` imports from in this environment, asked of the
# same python3 that `--install` hands to `-m pip`, so the answer and the
# install cannot be about two different environments.  Prints `absent`,
# `here`, or `elsewhere` followed by the directory on a second line.  The
# working directory is dropped from sys.path: `python3 -c` puts it first, and
# run from inside packages/aq-client it would answer `here` on any box.
installed_client_location() {
    python3 - "$CLIENT_DIR" <<'PY'
import importlib.util
import os
import sys

client_dir = os.path.realpath(sys.argv[1])
cwd = os.path.realpath(os.getcwd())
sys.path[:] = [p for p in sys.path if p and os.path.realpath(p) != cwd]
try:
    spec = importlib.util.find_spec("agent_queue_api_client")
except (ImportError, ValueError):
    spec = None
if spec is None or not spec.origin:
    print("absent")
else:
    found = os.path.dirname(os.path.dirname(os.path.realpath(spec.origin)))
    if found == client_dir:
        print("here")
    else:
        print("elsewhere")
        print(found)
PY
}

# A worker session's interpreter is the environment the daemon and every other
# slot share.  It is never offered the install, by flag or by hint: an agent
# that reads "to move the install here, run ..." is liable to run it.
WORKER=0
if [[ "${AQ_DB_SCOPE:-}" == "worker" ]]; then
    WORKER=1
fi

if [[ "$INSTALL" == 1 ]]; then
    if [[ "$WORKER" == 1 ]]; then
        echo "Error: --install is refused in a worker session (AQ_DB_SCOPE=worker)." >&2
        echo "       This session's pip belongs to an environment shared with the daemon and every" >&2
        echo "       other slot; an editable install from here would make all of them import the" >&2
        echo "       client from this slot, which is reset onto another branch between tasks." >&2
        echo "       Regenerate without --install: the contract tests read packages/aq-client/ from" >&2
        echo "       the checkout, and PYTHONPATH=packages/aq-client runs this tree's client." >&2
        exit 1
    fi
    if ! LOCATION="$(installed_client_location)"; then
        echo "Error: could not tell where agent-queue-api-client is installed, so --install cannot" >&2
        echo "       rule out re-pointing another tree's install. Is python3 on PATH?" >&2
        exit 1
    fi
    if [[ "$(head -n 1 <<<"$LOCATION")" == "elsewhere" ]]; then
        echo "Error: --install would re-point this environment's agent-queue-api-client, which" >&2
        echo "       currently imports from another tree:" >&2
        echo "         $(tail -n 1 <<<"$LOCATION")" >&2
        echo "       Everything else using this environment would follow it here.  If that is what" >&2
        echo "       you want, do it by hand:" >&2
        echo "         python3 -m pip install -e $CLIENT_DIR" >&2
        exit 1
    fi
fi

# The exact openapi-python-client the committed packages/aq-client/ tree was
# generated by.  See the note in the header before changing it.
GENERATOR_VERSION="0.29.0"

if ! command -v openapi-python-client >/dev/null 2>&1; then
    echo "Error: openapi-python-client is not installed. Run: pip install 'openapi-python-client==$GENERATOR_VERSION'" >&2
    exit 1
fi

FOUND_VERSION="$(openapi-python-client --version | tr -d '\r' | awk '{print $NF}')"
if [[ "$FOUND_VERSION" != "$GENERATOR_VERSION" ]]; then
    echo "Error: openapi-python-client $FOUND_VERSION is installed but packages/aq-client/ is generated by $GENERATOR_VERSION." >&2
    echo "       Regenerating with another version rewrites the whole client tree (README.md included)." >&2
    echo "       Run: pip install 'openapi-python-client==$GENERATOR_VERSION'" >&2
    echo "       (Deliberately upgrading? Bump GENERATOR_VERSION in this script and commit the regenerated client with it.)" >&2
    exit 1
fi

# The generator only *warns* when a post hook's command is missing ("Skipping
# Integration: ruff is not in PATH") and still exits 0, so a box without ruff
# would quietly write an unformatted agent_queue_api_client/ and record its
# digests as canonical.  Fail instead.
if ! command -v ruff >/dev/null 2>&1; then
    echo "Error: ruff is not installed, and the generator's post hooks format the generated Python with it." >&2
    echo "       Without it the client would be regenerated unformatted. Run: pip install -e '.[dev]'" >&2
    exit 1
fi

case "$MODE" in
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

# Record the digests of the generator-only boilerplate.  The tree check in
# tests/test_api_client_contract.py can only run where the pinned generator is
# installed and skips everywhere else, so on a box carrying just the `cli`
# extra it is silently a no-op -- which is how a hand-edited README.md once
# landed with both the pin and that check already in place.  These digests let
# the same files be verified from the checkout alone, on every box.  Keep the
# list in step with _GENERATOR_BOILERPLATE in that test; it asserts they agree.
DIGEST_FILE="$SCRIPT_DIR/aq-client-boilerplate.sha256"
(cd "$CLIENT_DIR" && sha256sum \
    README.md \
    pyproject.toml \
    agent_queue_api_client/__init__.py \
    agent_queue_api_client/client.py \
    agent_queue_api_client/errors.py \
    agent_queue_api_client/types.py \
    agent_queue_api_client/py.typed) > "$DIGEST_FILE"
echo "Recorded boilerplate digests at $DIGEST_FILE"

# The environment is only touched on request -- see the header.  PEP 668 marks
# some interpreters externally managed; the client is a dev artifact, so fall
# back rather than failing the regeneration.
if [[ "$INSTALL" == 1 ]]; then
    if python3 -m pip install -e "$CLIENT_DIR" --quiet \
        || python3 -m pip install -e "$CLIENT_DIR" --quiet --break-system-packages; then
        echo "Installed agent-queue-api-client from $CLIENT_DIR"
    else
        echo "WARNING: could not pip install $CLIENT_DIR — install it manually" >&2
    fi
else
    # A report, not a gate: the regeneration above already succeeded.
    LOCATION="$(installed_client_location 2>/dev/null || echo unknown)"
    case "$(head -n 1 <<<"$LOCATION")" in
        here)
            echo "agent-queue-api-client already imports from $CLIENT_DIR — nothing to reinstall."
            ;;
        elsewhere)
            echo "Left the environment alone: agent-queue-api-client imports from another tree,"
            echo "  $(tail -n 1 <<<"$LOCATION")"
            echo "so the client just regenerated is not the one 'import agent_queue_api_client' finds."
            echo "To run this tree's copy:  PYTHONPATH=$CLIENT_DIR <command>"
            if [[ "$WORKER" == 0 ]]; then
                echo "To move the install here (every user of this environment follows it):"
                echo "  python3 -m pip install -e $CLIENT_DIR"
            fi
            ;;
        absent)
            echo "agent-queue-api-client is not installed in this environment."
            if [[ "$WORKER" == 0 ]]; then
                echo "To install it:  python3 -m pip install -e $CLIENT_DIR    (or rerun with --install)"
            fi
            ;;
        *)
            echo "Could not tell where agent-queue-api-client is installed; left the environment alone."
            if [[ "$WORKER" == 0 ]]; then
                echo "To install this tree's copy:  python3 -m pip install -e $CLIENT_DIR"
            fi
            ;;
    esac
fi

echo "Done. Don't forget to commit packages/aq-client/, openapi.json and $DIGEST_FILE"
