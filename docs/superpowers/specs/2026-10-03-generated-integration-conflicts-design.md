# Generated artifacts in train merges

Task: `prime-vault-27`.

Parallel branches regenerate `tests/selection_catalogue.json` independently.
The catalogue is already marked `merge=aq-generated`, but child-to-parent
promotion and accepted CI repair rebuilds onto newer main use plain
`merge-tree` and escalate those overlaps as source conflicts.

Train merges must use the exact requested merge base and merged source tree.
When both sides change an artifact marked `merge=aq-generated` in that tree,
regenerate it in an isolated scratch worktree, including when Git reports a
clean text merge. For generated-only conflicts, restore the current side's
artifact in the scratch tree before regeneration. Missing current-side files
are restored as deletions. Mixed or source-only conflicts remain conflicts.

The shared mechanism belongs in `src/integration/regeneration.py`. Candidate
member construction, child promotion and accepted-CI-repair main rebuilds use
it. The development publisher retains its existing configured regeneration
policy. The regenerator defaults to `scripts/regenerate-generated.sh`, runs
with the existing timeout and database refusal environment, and may change
only paths marked generated. Failure retains the caller's conflict evidence
and existing repair route. Scratch worktrees are removed on every outcome.

The scratch subprocess environment is an allowlist with worker database
refusal sentinels. Its tool lookup order is the daemon interpreter's directory,
the daemon user's standard `~/.local/bin` installation directory, then
`/usr/local/bin`, `/usr/bin` and `/bin` (duplicates removed). This supports a
system-Python daemon with user-installed `openapi-python-client` and `ruff`
without inheriting ambient PATH, database URLs or credentials. The canonical
scripts retain their exact generator version and required-tool checks: a
missing tool or wrong version fails regeneration, and no installation or
editable-install mutation is performed.

Regeneration constructs a tree; it grants no publication authority. Existing
source review, branch fences, exact remote leases, ancestry, receipts and CI
checks still apply to the resulting commit. Both merge parents are retained.

Verification includes two branches regenerating a real selection catalogue
from different test modules, canonical combined output, preserved source
files and merge parents, replay, generated-only main rebuild, source/mixed
conflict refusal, failed regeneration, and rejection of non-generated writes.
