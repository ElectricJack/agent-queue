Authorized source CI repair for task {{task_id}}, PR {{source_pr_url}}.
Repository {{source_repository_id}}; source branch {{source_branch}}; exact source base {{source_base}}, head {{source_head}}, checkpoint generation {{source_generation}}.
Observed {{state}} under policy generation {{policy_generation}}.
Failing/cancelled checks:
{{checks}}

Repair this original task in place on its assigned source branch and existing PR. Preserve the recorded source head as an ancestor; resolve the concrete failed checks and regenerate generated artifacts from their sources. A cancelled check is infrastructure, never a passing check or a code defect. Run focused checks with aq test and publish this same branch with exact evidence. Do not create a separate repair task, branch or PR, and do not publish the default branch. This task has one automatic source-CI repair; a further red result goes to the project supervisor instead of spawning another repair.
