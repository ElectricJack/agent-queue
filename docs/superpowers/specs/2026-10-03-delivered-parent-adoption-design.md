# Delivered children can settle an obsolete parent aggregate

`aq integration adopt PROJECT --task PARENT --head-sha TARGET --reason REASON
--settle-delivered-children` is the explicit supervisor/operator recovery for a
managed parent whose child artifacts reached the repository's default branch by
other routes. `--dry-run` reports the proof and proposed verifier retirement
without writing. Ordinary adoption retains its verified-parent requirement.

Recovery observes every current direct child completion against one fresh Git
snapshot of the exact supplied default-branch SHA. Each child must be COMPLETED
and independently contained by ancestry or an immutable Git replacement record,
or have an explicit no-artifact completion. A settlement saying work is not owed
does not prove delivery. Missing provenance, pending sources and open children
refuse recovery. An equivalent replacement additionally requires
`--accept-equivalent`; the flag cannot waive this child proof. A task trailer
or a child branch tip alone does not identify a completed artifact.

The parent must be an unassigned PAUSED managed collector with a matching episode
and operation. Its aggregate verifier and repair delegates must be detached;
live sessions, retained claims, workspace locks, attached or uncertain owners,
unresolved writes, manual holds, open human gates and reconciler engine ownership
refuse recovery. Detached canonical reservations can be fenced and released.
This includes a child's `worker` reservation only when it is writerless, belongs
to that exact child's canonical branch in the designated repository, and the
child's current completion passes the same default-branch delivery proof.
Reservations on unrelated branches, the default branch, or owned by another
task remain binding. Pending external writes on child branches also refuse.
Dry runs name every owner and its fence proposed for release; apply releases
them in the settlement transaction and retains those identities in its audit.
The project hierarchy lock excludes claims while the complete task, child,
checkpoint, operation, delegate and owner identities are rechecked. Git target
freshness is rechecked before the transaction commits.

Apply cancels the obsolete collection operation and its outstanding stages,
retires detached delegates through the existing audited delegate release, and
completes the parent through the operator-adoption transition. It retains the
target SHA as the parent's operator completion source in Git. An audit event
binds that completion to the original episode, operation, checkpoint generation
and new checkpoint version, with every child source/proof and the operator's
reason. Historical verifier subjects, receipts and check evidence stay intact.
No successful CI, parent verification or verified-operation completion is added.

Delivery readers recognize this exact audited operator generation and evaluate
its immutable Git source against their current target. A reopen, newer close or
changed checkpoint invalidates the adoption binding. It is never a fallback for
a damaged verified-parent binding or an ordinary leaf completion.

`aq doctor --check integration.delivered_children_unsettled_parent` names these
paused parents and reports current Git proof, verifier identity, blockers and
the guarded dry-run command. Doctor is report-only. Regression tests use real
Git and disposable PostgreSQL, including the agile-harbor-62 incident shape:
four independently delivered children, a stale unmaterialized READY verifier,
and no verified parent completion. Live application belongs to the supervisor;
a worker must not mutate another task or restart the operator daemon.

During the reconciler migration, this diagnostic is a gated legacy check in
`LEGACY_INTEGRATION_DOCTOR_CHECKS`. Its replacement is the subject's observer
fact in `aq integration status`, and it shares the legacy engine cutover removal
gate. The migration inventory therefore includes twenty-one legacy checks;
the three approved reconciler checks remain unchanged.
