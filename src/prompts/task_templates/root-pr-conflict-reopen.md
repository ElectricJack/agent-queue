The train could not mechanically merge {{ default_ref }} at {{ default_sha }}
into your original branch {{ source_branch }} at {{ source_sha }}.
Resolve these source conflicts on this same task and branch; preserve the
original change and merge history.

{{ conflicting_paths }}

Fetch origin and merge the default branch (do not rebase). Regenerate generated
artifacts with the repository's regenerating merge drivers. Run targeted tests
for the touched files only; do not run full CI or the full suite. Push and close
this task again. The batch tip runs the full integration gate.

PR: {{ pr_url }}
Head/default pair: {{ pair_key }}
