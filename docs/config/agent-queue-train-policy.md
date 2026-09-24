# Agent Queue train policy handoff

The project-scoped bundles in `src/prompts/reviewed_playbooks/` are compiled
against the current V2 command and event registries. The parent route handles
the current `integration_complete_parent` outcomes, including idempotent
`already_completed` and terminal `failed`. The policy JSON binds their exact
artifact identities and the four `Tests (...)` matrix checks from
`.github/workflows/tests.yml` to the `github-actions` producer.

The bundles are copied to `vault/reviewed-playbooks/<id>/` when the deployed
daemon seeds reviewed bundles. Only a project-scoped supervisor or local
operator should run the following commands. Review the manifest and source
first. Do not change the project's development mode until the approved cutover
prerequisites allow a drain; that mode is the current publisher's validation
gate. Use the fresh integration generation for every mutation.

```bash
aq playbook v2-validate --path reviewed-playbooks/agent-queue-parent-integration/artifact.json
aq playbook v2-validate --path reviewed-playbooks/agent-queue-root-train/artifact.json
aq playbook v2-import --path reviewed-playbooks/agent-queue-parent-integration
aq playbook v2-import --path reviewed-playbooks/agent-queue-root-train
aq playbook activate --playbook-id agent-queue-parent-integration --artifact-sha256 sha256:a6a39410cf600b46cf90619966d604ae99284d48cf037f60b575a8d1068532eb --enabled
aq playbook activate --playbook-id agent-queue-root-train --artifact-sha256 sha256:ba3c982468daebe36942083b23c5662d8f744815723286d2e5adc32b998b88dd --enabled
aq playbook activation-health --playbook-id agent-queue-parent-integration
aq playbook activation-health --playbook-id agent-queue-root-train
```

Both validations should have zero errors or questions. Imports must report the
two hashes above. Activation health must report each exact hash as enabled and
ready. A stale contract or activation refusal is a blocker; do not change the
policy snapshot to conceal it.

After the development publisher is safely drained and the project can enter
disabled mode, bind the repository, review mode, and policy in order. The
repository ID `agent-queue2` comes from the existing development deliveries;
verify that its stored GitHub origin and default branch are exact before
binding it. The helper reads a fresh generation for each command, avoiding a
stale CAS value.

```bash
generation() {
  aq --json integration status agent-queue |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["generation"])'
}

aq integration status agent-queue
aq integration enable agent-queue --mode disabled --expected-generation "$(generation)" --reason 'drain development publisher for train policy binding'
aq integration status agent-queue
aq project set agent-queue integration-repository-id agent-queue2 --expected-integration-generation "$(generation)" --reason 'bind exact existing repository'
aq project set agent-queue integration-review-mode pull_request --expected-integration-generation "$(generation)" --reason 'require human PR review'
policy_json="$(python3 -c 'import json; print(json.dumps(json.load(open("docs/config/agent-queue-train-policy.json")), separators=(",", ":")))')"
aq project set agent-queue integration-policy "$policy_json" --expected-integration-generation "$(generation)" --reason 'bind reviewed train policy and artifacts'
aq integration enable agent-queue --mode observe --expected-generation "$(generation)" --reason 'preflight train configuration without scheduling'
aq integration flush agent-queue
aq integration status agent-queue
```

The status response in observe mode must report zero functional blockers and
`ready: true`; activation health above confirms the exact active route hashes.
Observe does not schedule train batches. The later train-mode cutover belongs to the supervisor after
scratch proof and a green `main`; this handoff does not authorize it.
