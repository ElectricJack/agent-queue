# Task11c fix round1 review — 6b0101be

Original findings1/2/3/5/6 ADDRESSED. #4 NOT ADDRESSED due to real token permission mismatch below. Bounded reserved-delegate resumed-event→writer verification RESOLVED: exact never-started reservation succeeds; attached live delegate remains blocked, resume-only exception. No out-of-scope observations. Reviewer ran no tests/writes; exact13pass11inheritedwarnings gate checked.

## Open Important finding (verbatim)

**Hosted-variable preflight is incompatible with actual installation-token permissions.** `src/integration/preflight.py:58` calls `GET /repos/{owner}/{repo}/actions/variables/{name}`. GitHub requires **Variables repository read** permission for this endpoint. [GitHub endpoint documentation](https://docs.github.com/en/rest/actions/variables#get-a-repository-variable). The actual client requests `_PERMISSIONS` without `variables` (`src/git/github_app.py:26`, `:452`) and rejects additional permissions (`:465`). Consequently, correctly configured deployments receive `hosted_workflow_variables_unavailable` and cannot enable managed modes. The test client bypasses token minting (`tests/test_integration_operational_controls.py:459`). Provide the required narrowly scoped read permission through the real client’s permission contract and add a permission-aware fake-transport regression; no live permission mutation is needed for this fix.

## Fix2 scope

Add required variables:read to exact installation-token permission contract and update matching pinned-permission tests/fixtures; retain repository scope, no extra permissions or production mutation. Add real GitHubAppClient + fake transport preflight regression that enforces variables permission. Update downstream CLI guide handoff to include operator-managed App Variables read permission. Cover only amended preflight/App client tests; no area-gate repeat. Previous review HEAD6b0101be is fix2 base.
