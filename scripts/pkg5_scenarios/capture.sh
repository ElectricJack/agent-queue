#!/usr/bin/env bash
# Capture the seven §13.3 manual-review screenshots.
#
# Prerequisites (all offline — no daemon, no database, no migrations):
#   npm install
#   ./scripts/regenerate-ts-client.sh --from-file
#   python scripts/pkg5_scenarios/build_payloads.py
#   (cd dashboard && npx vite --host 127.0.0.1 --port 5199)
#
# Usage: scripts/pkg5_scenarios/capture.sh [PORT] [OUT_DIR]
set -euo pipefail

PORT="${1:-5199}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${2:-$ROOT/docs/superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios}"
BASE="http://127.0.0.1:${PORT}/scenarios.html?scenario="
GRAPH='[aria-label="Playbook semantic graph"]'

mkdir -p "$OUT"
ab() { agent-browser "$@"; }

load() {  # load <scenario> [event-scope]
    ab open "${BASE}$1" >/dev/null
    ab wait --text "Activation" >/dev/null
    if [[ -n "${2:-}" ]]; then
        ab select "#\\:r0\\:" "$2" >/dev/null 2>&1 || ab eval "
            (() => {
              const select = document.querySelector('select');
              const setter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value').set;
              setter.call(select, '$2');
              select.dispatchEvent(new Event('change', { bubbles: true }));
              return select.value;
            })()" >/dev/null
        ab wait 800 >/dev/null
    fi
    ab find role button click --name "Fit View" >/dev/null
    ab wait 600 >/dev/null
}

set_select() {  # set_select <element-id> <value> — React-safe native setter
    ab eval "
        (() => {
          const select = document.getElementById('$1');
          const setter = Object.getOwnPropertyDescriptor(
            window.HTMLSelectElement.prototype, 'value').set;
          setter.call(select, '$2');
          select.dispatchEvent(new Event('change', { bubbles: true }));
          return select.value;
        })()" >/dev/null
    ab wait 1200 >/dev/null
    ab find role button click --name "Fit View" >/dev/null
    ab wait 600 >/dev/null
}

# The v6 candidate's hash, as the activation chooser lists it.
V6_SHA="$(python3 - <<'PYEOF'
import json, pathlib
payloads = json.loads(
    pathlib.Path("dashboard/scenarios/payloads/06-diff-review.json").read_text()
)
print(
    next(
        entry["artifact"]["artifact_sha256"]
        for entry in payloads["/api/playbook/artifacts"]["artifacts"]
        if entry["artifact"]["version"] == 6
    )
)
PYEOF
)"

ab set viewport 1600 1100 >/dev/null
ab set media dark >/dev/null

# 1 — branching: classify-risk's seven labelled outgoing edges.
load 01-branching task.completed
ab screenshot "$GRAPH" "$OUT/01-branching.png"

# 2 — convergence: classify-risk:low and escalate:completed both enter await-approval.
#     Zoomed and with the `low` edge selected, because at fit-to-view the two
#     labels sit in the same crowded band and §13.3 asks for both to be legible.
load 02-convergence task.completed
for _ in 1 2 3; do ab find role button click --name "Zoom In" >/dev/null; done
ab wait 600 >/dev/null
ab click '[aria-label="success edge from classify-risk to await-approval on outcome low"]' >/dev/null
ab wait 500 >/dev/null
ab screenshot "$GRAPH" "$OUT/02-convergence.png"

# 3 — loop: for-each-task's body, check-gate's loop_back, and overlay traversal counts.
load 03-loop spec.approved
set_select v2-run run-7
ab screenshot "$GRAPH" "$OUT/03-loop.png"

# 4 — AI node: the inspector's profile, capabilities, fingerprint, budget and schema.
ab set viewport 1600 2400 >/dev/null
load 04-ai-node task.completed
ab click '[aria-label^="Inspect step Classify review risk"]' >/dev/null
ab wait 500 >/dev/null
ab find role button click --name "Advanced" >/dev/null
ab wait 500 >/dev/null
ab screenshot '[aria-label="Node inspector"]' "$OUT/04-ai-node.png"
ab set viewport 1600 1100 >/dev/null

# 5 — stale contract: diagnostics visible, graph still fully drawn.
load 05-stale-contract
ab screenshot --full "$OUT/05-stale-contract.png"

# 6 — diff review: v5 → v6, executable vs presentation-only, activate gated.
load 06-diff-review
set_select v2-artifact "$V6_SHA"
ab screenshot --full "$OUT/06-diff-review.png"

# 7 — run overlay: a v5 run while v6 is active.
load 07-run-overlay-old-artifact
set_select v2-run run-7
ab screenshot --full "$OUT/07-run-overlay-old-artifact.png"

echo "captured 7 screenshots into $OUT"
