# Recompile playbooks with legacy worker profiles

An older Playbooks V2 `agent_task` could name a worker rung such as
`fast-low-claude` in `profile_id`. Current routing accepts a role profile there
only. A worker task instead supplies an `intelligence_class` hint; the router
chooses its provider and worker profile.

The proposal compiler migrates a legacy `<class>-<harness>` worker rung to its
class hint. It also moves a matching literal `inputs.intelligence_class` to the
step field. Conflicting hints fail compilation. Stored artifacts remain strict
and immutable: compiling produces a new hash; loading the old hash never
silently changes its meaning.

## Repair `quilt-health-watch`

Run these steps from an operator session **after** the code containing this
migration is deployed. The project supervisor can coordinate the review, but
the worker token cannot import or activate a project playbook.

1. In the vault's `projects/quilt-trader/playbooks/quilt-health-watch.md`,
   change the check task's profile wording to class `fast-low`. Keep the rest
   of the health check procedure intact. The existing
   `drafts/quilt-health-watch/semantic-body.json` can be used as the proposal
   input; its three old profile pins are migrated by the compiler.
2. Generate a candidate in a new directory outside the live vault:

   ```bash
   python scripts/migrate-legacy-playbook-worker-routes.py \
     --vault-root ~/.agent-queue/vault \
     --source ~/.agent-queue/vault/projects/quilt-trader/playbooks/quilt-health-watch.md \
     --semantic-body ~/.agent-queue/vault/drafts/quilt-health-watch/semantic-body.json \
     --baseline-artifact ~/.agent-queue/vault/reviewed-playbooks/quilt-health-watch/artifact.json \
     --output /tmp/quilt-migration/quilt-health-watch
   ```

   The script compiles with the current built-in command, event and shipped
   profile registries. It never connects to the daemon database. The baseline
   is read only for versioning and semantic diff. Expect three
   `legacy_worker_route_migrated` warnings, three agent tasks with no
   `profile_id` and `intelligence_class: fast-low`, no errors or
   questions, and a new version and hash. Inspect `migration-report.json` and
   the complete new artifact. Live registry validation still happens at import.
3. Review and approve the candidate, replacing the candidate manifest's
   placeholder prose with the new review record. Preserve the previous bundle
   for audit, then copy the approved candidate directory into
   `vault/reviewed-playbooks/quilt-health-watch/`. The script writes canonical
   `artifact.json`, the full `artifact.sha256`, updated `source.md`, and the
   manifest frontmatter, including `profiles_referenced`. The old worker
   profile must be absent.
4. Validate, import and activate the newly reviewed hash:

   ```bash
   aq playbook v2-validate --path reviewed-playbooks/quilt-health-watch/artifact.json
   aq playbook v2-import --path reviewed-playbooks/quilt-health-watch
   aq playbook activate --playbook-id quilt-health-watch --artifact-sha256 sha256:<new-hash>
   aq playbook activation-health --playbook-id quilt-health-watch
   aq doctor --check playbooks.activation_invalid
   ```

Activation accepts a valid replacement when the currently active artifact is
classified `invalid`, even though the old definition cannot be parsed for a
diff. The previous hash remains in the activation response for audit. Confirm
that activation health is `ready` and that `playbooks.activation_invalid`
reports no invalid enabled activations.
