#!/usr/bin/env bash
# check-merge-conflicts.sh — Detect merge conflicts between task branches and main
#
# Usage: check-merge-conflicts.sh <repo-path>
#
# Outputs JSON with conflict information for each branch that cannot be
# cleanly merged into main. Exits 0 if no conflicts, 1 if conflicts found.

set -euo pipefail

REPO_PATH="${1:-.}"
cd "$REPO_PATH"

# Ensure we have the latest remote state
git fetch origin --prune --quiet 2>/dev/null

MAIN_BRANCH="main"
MAIN_REF="origin/$MAIN_BRANCH"

# Verify main exists
if ! git rev-parse "$MAIN_REF" >/dev/null 2>&1; then
    echo '{"error": "Cannot find origin/main"}'
    exit 2
fi

conflicts=()
checked=0
clean=0

# List all remote branches except main and HEAD
while IFS= read -r branch_ref; do
    branch_ref=$(echo "$branch_ref" | xargs)  # trim whitespace
    [ -z "$branch_ref" ] && continue

    # Skip the symbolic HEAD pointer line ("origin/HEAD -> origin/main");
    # newer git sets origin/HEAD on fetch, so it appears in CI but not in
    # older local clones.
    case "$branch_ref" in
        *" -> "*) continue ;;
    esac

    # Strip origin/ prefix to get branch name
    branch_name="${branch_ref#origin/}"

    # Skip main, HEAD pointers, and dependabot branches
    case "$branch_name" in
        main|HEAD|dependabot/*) continue ;;
    esac

    checked=$((checked + 1))

    # Try a merge in-memory using git merge-tree (available in git 2.38+)
    # Fall back to a throwaway merge if merge-tree is not available
    merge_base=$(git merge-base "$MAIN_REF" "$branch_ref" 2>/dev/null || echo "")
    if [ -z "$merge_base" ]; then
        # No common ancestor — skip
        continue
    fi

    # Use merge-tree to check for conflicts without touching the worktree
    merge_output=$(git merge-tree "$merge_base" "$MAIN_REF" "$branch_ref" 2>/dev/null || true)

    if echo "$merge_output" | grep -q "^+<<<<<<< "; then
        # Extract conflicting file names from merge-tree output
        conflicting_files=$(echo "$merge_output" | grep -E "^changed in both" | sed 's/^changed in both//' | xargs || echo "unknown files")

        # Extract task ID from branch name (format: task-id/description)
        task_id=""
        description=""
        if [[ "$branch_name" == */* ]]; then
            task_id="${branch_name%%/*}"
            description="${branch_name#*/}"
        else
            task_id="$branch_name"
            description="$branch_name"
        fi

        # Get the last commit info on this branch
        last_commit=$(git log -1 --format="%h %s" "$branch_ref" 2>/dev/null || echo "unknown")
        behind_count=$(git rev-list --count "$branch_ref".."$MAIN_REF" 2>/dev/null || echo "?")

        conflicts+=("{\"branch\": \"$branch_name\", \"task_id\": \"$task_id\", \"description\": \"$description\", \"conflicting_files\": \"$conflicting_files\", \"last_commit\": \"$last_commit\", \"commits_behind_main\": $behind_count}")
    else
        clean=$((clean + 1))
    fi
done < <(git branch -r --list 'origin/*')

conflict_count=${#conflicts[@]}

# Build JSON output
if [ "$conflict_count" -eq 0 ]; then
    echo "{\"status\": \"clean\", \"checked\": $checked, \"clean\": $clean, \"conflicts\": []}"
    exit 0
else
    # Join conflict entries with commas
    joined=""
    for i in "${!conflicts[@]}"; do
        if [ "$i" -gt 0 ]; then
            joined="$joined, "
        fi
        joined="$joined${conflicts[$i]}"
    done
    echo "{\"status\": \"conflicts_found\", \"checked\": $checked, \"clean\": $clean, \"conflict_count\": $conflict_count, \"conflicts\": [$joined]}"
    exit 1
fi
