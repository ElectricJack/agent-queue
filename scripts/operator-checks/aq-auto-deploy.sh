#!/usr/bin/env bash
# Auto-deploy: keep the running Agent Queue on origin/dev (Jack 2026-10-12:
# "when changes land on dev, agent queue gets updated"). Run from cron every
# 5 minutes. Fast-forward only; refuses a dirty tree, a non-dev branch, or
# multiple alembic heads, and logs why. Rebuilds the dashboard when dashboard/
# changed.
#
# A refusal used to be log-only, so uncommitted edits in the live checkout
# stalled every deploy for ~17 hours with no alert (2026-10-14, bold-stone-86).
# Consecutive skips are now counted in $STATE; after $SKIP_ALERT_RUNS of them
# the supervisor gets one message per stall, and a clean deploy (or a clean,
# current tree) resets the count. The stall sweep reports the same stall from
# the log. Agents must never edit the live checkout: work in a worktree.
#
# Canonical copy: scripts/operator-checks/aq-auto-deploy.sh. The cron entry runs
# the installed copy in ~/.agent-queue/operator-checks/; the operator diffs and
# installs it (docs/specs/operator-stall-sweep.md).
set -uo pipefail
REPO=${AQ_AUTO_DEPLOY_REPO:-/home/jkern/dev/agent-queue2}
LOG=${AQ_AUTO_DEPLOY_LOG:-/home/jkern/.agent-queue/logs/aq-auto-deploy.log}
STATE=${AQ_AUTO_DEPLOY_STATE:-/home/jkern/.agent-queue/aq-auto-deploy.skips}
LOCK=${AQ_AUTO_DEPLOY_LOCK:-/tmp/aq-auto-deploy.lock}
SKIP_ALERT_RUNS=${AQ_AUTO_DEPLOY_SKIP_ALERT_RUNS:-6}  # 6 runs = 30 minutes
SUPERVISOR=${AQ_AUTO_DEPLOY_SUPERVISOR:-session:supervisor-agent-queue}
export USER=jkern HOME=/home/jkern
export PATH=${AQ_AUTO_DEPLOY_PATH:-/home/jkern/.local/bin:/usr/local/bin:/usr/bin:/bin}
exec 9>"$LOCK"; flock -n 9 || exit 0
log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

# One line in $STATE: "<first skip epoch> <consecutive skips> <alerted 0|1>".
skip() {
  local first=0 count=0 alerted=0 behind since
  [ -r "$STATE" ] && read -r first count alerted < "$STATE"
  [ "$count" -gt 0 ] 2>/dev/null || { first=$(date +%s); count=0; alerted=0; }
  count=$((count + 1))
  log "skip: $1 (run $count)"
  if [ "$count" -ge "$SKIP_ALERT_RUNS" ] && [ "$alerted" != 1 ]; then
    behind=$(git rev-list --count HEAD..origin/dev 2>/dev/null || echo "?")
    since=$(date -u -d "@$first" +%FT%TZ)
    if (unset AQ_API_TOKEN; aq message send --to "$SUPERVISOR" \
        --subject "Auto-deploy stalled: $1" \
        --body "Auto-deploy of $REPO has skipped $count consecutive runs since $since: $1. \
origin/dev is $behind commits ahead of the running daemon and nothing deploys until this \
is fixed. Agents must never edit the live checkout. Save any edits first (a rescue branch \
plus a copy under ~/.agent-queue/backups/), restore a clean tree on dev, and the next run \
deploys. Log: $LOG") >> "$LOG" 2>&1; then
      alerted=1; log "escalated: $count consecutive skips reported to $SUPERVISOR"
    else
      log "escalation to $SUPERVISOR failed; retrying next run"
    fi
  fi
  echo "$first $count $alerted" > "$STATE"
  exit 0
}

cd "$REPO" || exit 1
git fetch -q origin dev || { log "fetch failed"; exit 1; }
[ "$(git branch --show-current)" = dev ] || skip "checkout not on dev"
OLD=$(git rev-parse HEAD); NEW=$(git rev-parse origin/dev)
DIRTY=$(git status --porcelain --untracked-files=no)
if [ "$OLD" = "$NEW" ]; then
  # Nothing to deploy: only a clean tree ends a stall, since dirty edits
  # would block the next commit that lands on dev.
  [ -n "$DIRTY" ] || rm -f "$STATE"
  exit 0
fi
if [ -n "$DIRTY" ]; then
  skip "dirty tree, uncommitted edits to $(echo "$DIRTY" | awk '{print $NF}' | head -5 | paste -sd ' ')"
fi
git merge -q --ff-only origin/dev || skip "dev is not a fast-forward of $OLD"
HEADS=$(.venv/bin/alembic heads 2>/dev/null | grep -c head)
if [ "$HEADS" != 1 ]; then log "BLOCKED: $HEADS alembic heads at $NEW; rolled back"; git reset -q --hard "$OLD"; exit 1; fi
rm -f "$STATE"
N=$(git rev-list --count "$OLD..$NEW")
bash /home/jkern/.agent-queue/operator-checks/aq-restart-scrubbed.sh >> "$LOG" 2>&1 || log "restart reported failure"
# A deploy can change a required playbook's contract and leave the router
# inactive (stale_contract, 2026-10-12): re-activate before declaring success.
(unset AQ_API_TOKEN; aq doctor --check playbooks.required --fix >> "$LOG" 2>&1) || log "playbooks.required fix failed"
if git diff --quiet "$OLD" "$NEW" -- dashboard/; then :; else
  .venv/bin/python /home/jkern/.agent-queue/operator-checks/build-dashboard.py >> "$LOG" 2>&1 && \
    (unset AQ_API_TOKEN; aq dashboard restart >> "$LOG" 2>&1)
fi
log "deployed $N commits ${OLD:0:9} -> ${NEW:0:9} health=$(curl -s http://127.0.0.1:8081/health | head -c 80)"
