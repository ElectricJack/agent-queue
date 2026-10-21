# Assigned batch repair publication

A supervisor-filed fixer can work directly on an existing Git-first batch
candidate without replacing its canonical task branch or allocating another
automatic repair attempt. The supervisor or local operator explicitly assigns it
with `aq task set TASK --repair-batch BATCH`. Task descriptions and worker-written
metadata never confer this authority.

The assignment binds the task, project, integration repository and candidate ref
of one open batch. It grants push access only, while the session holds that task;
it does not grant PR, default-branch or promotion publication rights. Reassignment
and abort take effect before the next publication. Raw writes to the reserved
assignment metadata key are refused.

A repair push defaults to the assigned candidate, revalidates the assignment
under a bounded per-ref lease, and must fast-forward the exact current remote
candidate. Its existing history is the validation base; only added commits are
subject to task identity and reserved-path checks. Caller-provided expected OIDs
must match that observed candidate and cannot enable a rewrite. Another live
writer, unavailable/missing ref, stale assignment or divergent history refuses
publication.

Successful candidate pushes wake the matching delivery target. The next train
visit fetches the branch and evaluates checks for its new exact SHA, including
when automatic repair attempts are exhausted. The wake carries no green evidence
and does not reset the attempt budget or override operator intent.
