# Operator workers

An operator worker performs project setup through an audited, bounded command
surface while retaining its session identity. It holds one `operator` task and
uses the same pool, claim, workspace and flock lifecycle as a coding worker.
It does not become the loopback human or an elevated supervisor.

The shipped `operator-claude` and `operator-codex` templates derive a separate
pool rung for each of `standard-high` and `deep-high`. They start with
`min_active: 0`, `max_active: 1`; size them with `aq pool scale` as usual.
The explicit `operator_execute` grant identifies the role. Normal workers
never receive that grant. Operator profiles cannot run on local, cheap or
other harnesses, and operator capacity never substitutes for coding capacity.

The default assignment router has an `operator` kind rule. File setup,
cutover, trust/CI, promotion configuration, playbook lifecycle, provider
diagnostics and project policy imports as `--type operator`. A missing operator
rule or missing operator capacity leaves the task unrouted rather than assigning
an ordinary worker. An existing project's installed router is operator-owned:
import and activate the newly reviewed routing bundle through the normal
installation workflow before filing this kind. Installed vault files are
write-if-absent; changing shipped Markdown alone does not update an installation.

When a route reports `operator_rule_missing`, inspect the executed artifact's
`task_route_plan` policy literal, rather than only the editable vault source.
The result includes that policy's `policy_sha256`. Import and activate the reviewed
bundle containing `kinds.operator`, then reroute the affected unheld tasks through
the authorized supervisor/operator workflow. The worker doing a code fix cannot
activate a system router or reroute tasks it does not hold.

`aq pool provider status` lists operator profiles in diagnostics with `reason=role`
because bulk provider allocation controls select ordinary workers only. That
diagnostic does not exclude operator profiles from routing or pool startup;
inspect their individual bounds and demand with `aq pool status`.

Pool claim validation preserves the profile's operator role when checking the
session's frozen launch class and model. Demand counts only claimable routes
whose task kind agrees with that role and whose class agrees with the pool's
fixed class. An unrouted task or a stale route with a different role or class
cannot launch a pool session as queued demand. Explicit warm-pool floors still
apply.

## Preflight and execution

Declare the complete action list and every applicable prerequisite up front.
The preflight validates command names, available CLI options, types, required
arguments, project targets and human decision bindings. For repository setup it
also checks trust, GitHub App access/identity, branches and online runner labels.
Supply a candidate hosted policy/repository when the project has not bound them
yet. It records one report on the task and sends all blockers in one message to
the dashboard user. Check `ready`; a successful command envelope can contain a
blocked report. Resolve the full report before starting mutations.

```bash
aq operator preflight \
  --actions '[{"command":"integration_trust_manifest","arguments":{}},{"command":"integration_app_verify","arguments":{}}]' \
  --branches '["main","development"]' \
  --runner-labels '["self-hosted","matter-engine"]' --app-ids '[123456]'

aq operator execute --command integration_trust_manifest --arguments '{}'
```

Mutations require `--preflight-id` from a ready report issued within ten minutes
for the same task, session, claim epoch, exact command arguments and decision id.
Reads can run without a preflight. Use JSON arguments for the inner command; the
wrapper supplies `project_id` only when that command accepts it. Any explicitly
supplied project must match the held task.

## Human gates

The allowlist contains integration status/trust/App/cutover diagnostics, provider
status and policy diff; integration/promotion project edits, cutover, batch abort,
eject, origin retirement and owner release; project playbook import, activation,
pause/resume and deletion; and project policy apply. All ordinary command grants,
generation/hash fences, active human holds and disabled/drained requirements
still apply. Policy imports cannot alter global files or agent settings.
Playbook activation/resumption requires an approved document review of the exact
artifact and project. Global/system/agent-type playbooks are outside this role.

Project integration/promotion edits, cutover, destructive integration controls and
playbook deletion additionally require `--decision-id`. The local human records a
`note` on the operator task with `aq decision record`. Its `decision` is a JSON
object containing `command`, `args_sha256` and `claim_epoch`. The wrapper's refusal
prints the required binding. The digest is SHA-256 over UTF-8 JSON arguments with
sorted keys, separators `,`/`:`, and no NaN, including the injected project id.
The decision must be recorded by `human:local-operator`; a supervisor's attribution
or prose quoting a human is not authorization. A new claim or changed arguments
requires a new matching decision. The human sees the concrete action before
authorizing it.

```bash
# Human, after inspecting the complete arguments and binding from preflight:
aq decision record --object-kind task --object-id OPERATOR_TASK \
  --effect note --operator Jack --source cli --source-ref operator-setup \
  --idempotency-key UNIQUE_KEY \
  --decision '{"command":"playbook_delete","args_sha256":"EXACT_DIGEST","claim_epoch":1}'

# Operator: include that decision in the action list and rerun preflight first.
aq operator execute --command playbook_delete --arguments 'EXACT_JSON_ARGUMENTS' \
  --decision-id DECISION_ID --preflight-id PREFLIGHT_ID
```

The operator cannot approve reviews, change review delegation, approve gates or
promotions, attest decisions for the human, manage money/live trading, deploy,
merge, edit credentials or change global provider configuration. Route provider
diagnostics and portable project preferences to it; global provider changes need
the human. Shell access does not confer authority to bypass AQ, borrow a human
token/login, edit the database or write directly to managed vault files.
This command boundary is not an operating-system sandbox.

Every accepted action persists `operator.action` before dispatch and
`operator.result` afterward, linked to the held task, agent, session and claim.
Refusals with a verified claim persist `operator.refused`. Arguments are represented
by a digest rather than copied into the audit. Audit persistence failure prevents
dispatch. `operator.preflight` retains the input manifest and aggregate report.

## Review hygiene and rollout

Review revision refuses unchanged bodies (including frontmatter-only edits),
prose whitespace-only edits, and a change note declared formatting/progress-only.
It points the author to `aq task comment`. Substantive body, artifact or spec-kind
changes can create a revision; code whitespace remains significant. Reopening a
withdrawn review intentionally restores its existing revision content and gate.

After delivery, the integration owner updates the installed router/profile
catalogue and reroutes the remaining activation steps of `fleet-ridge-94` as an
operator task. The worker implementing this feature cannot reroute a task it
does not hold. Gather its complete action/prerequisite manifest before launch,
retain its existing exact artifact review and human decisions, and use the
operator preflight for activation. This delivery does not itself authorize an
activation or cutover.
