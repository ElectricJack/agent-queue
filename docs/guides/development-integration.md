# Development integration

For normal development, enable a project with focused local validation:

```bash
aq integration develop agent-queue --validation focused \
  --command 'aq test tests/test_development_integration.py' \
  --reason 'Use batched development delivery'
aq integration sweep agent-queue
aq integration status agent-queue
```

Commands run in AQ's retained integration checkout, using the daemon's environment. Install the project test dependencies there or use an absolute interpreter/helper path. A focused policy requires at least one command. `advisory` records failures while permitting publication; `none` records that validation was not run. Configure an hourly cadence with `--interval-seconds 3600`. Policy changes apply to the next batch without a drain. Multiple-repository projects must designate their integration repository first.

Workers push clean source branches and close with their actual checks. Ordinary commits and merges are accepted. Parent assembly and main publication happen separately; no intermediate verifier, PR, hosted-CI receipt chain or squash is required. Strict hierarchy/train remain available for projects that need the older workflow.

AQ retains candidate refs and a delivery journal. One publisher per repository validates the batch and advances main only if its previous head still matches. A competing update is retried against current main. A crash after push is reconciled by observing the remote. Conflicting members and dependents stay parked while independent work proceeds. Changed source commits are eligible automatically; `aq integration sweep PROJECT --retry` retries unchanged parked content under current policy.

Failed integration creates an ordinary repair task on its own branch, with three task retries and at most three generations of repair tasks. Queue and provider wait time do not expire a fixed repair episode. After the budget is exhausted, inspect the parked revision and explicitly retry or repair it.

To reconcile work already merged by an operator:

```bash
aq integration adopt PROJECT --task TASK --task PARENT \
  --head-sha ACTUAL_REMOTE_SHA --reason 'Reviewed and delivered on main'
```

The remote SHA must match. Source ancestry is verified. For squashed or manually rewritten equivalents, explicitly add `--accept-equivalent` after reviewing the delivered content. The journal calls this operator acceptance, never CI attestation. Open children and active task assignments still require resolution.

Cancel obsolete repair scheduling with `aq integration cancel-preserving OPERATION --reason TEXT`. Detached reservations are released; stopped attached writers require real provider termination proof. Their refs and workspaces remain retained. Delegate tasks are paused. In development projects, automatic preservation disables a stopped writer's checkout before unlocking it, so dirty files cannot be recycled into the next task. Use normal explicit workspace cleanup later.

Development mode is the replacement execution path, not a destructive rewrite of old audit tables. Existing strict episodes, App attestation support and cleanup records remain compatible. A migration downgrade refuses to discard a used development journal.
