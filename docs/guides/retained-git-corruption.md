# Retained Git corruption

Retained integration repositories cache origin and AQ's own branches. The existing
integration owner automatically attempts bounded recovery of confirmed corruption,
following approved review `rev-swift-flare` revision 2. This source change is not
evidence that a particular live cache has recovered.

The actual failing cache may be the retained development repository, its separate
delivery observer, or a bare repository reader. Recovery uses that cache's normal
repository transaction; it does not repair a worker checkout or replace a clone
that holds unpublished history.

On an integrity failure, new batches pause for the affected project. Existing
frozen inputs, owners, checkpoints and operator pauses remain intact. Other
projects continue. Regular empty or Git-diagnosed corrupt loose objects move to
a timestamped backup inside the Git directory's `aq-empty-object-backups`, outside
active `objects`. Bytes are preserved. The backup's `manifest.json` and current
`incident.json` record every move, attempt and scrubbed before/after fsck result.
The pending marker and incident survive interruption and daemon restart.

Origin is fully refetched using the configured authenticated transport without
negotiating against damaged tips. Readable local-only AQ heads are published
through the guarded absent-head publication path. Existing remote heads are not
forced, and non-AQ branches are not published. Full `fsck` verifies content;
connectivity-only checking is insufficient. Zero exit with integrity diagnostics
still fails.

On success the incident's admission hold clears and the existing owner retries
its normal batch flow. It still needs its ordinary validation and publication
gates to deliver. On failure the backup and hold remain, and the project supervisor
receives one coalesced incident notice with diagnostics and backup location. At
most two attempts are allowed, including interrupted/cancelled attempts. Restart
or another visit does not reset an exhausted incident.

An ordinary authentication/network error with a clean cache does not move objects.
Pack corruption, unsafe or unidentified object paths, unreadable local-only history,
failed publication, timeout or failed final checking require intervention. Read
the notice and manifest; preserve them and all original bytes. Do not delete the
journal to reset the cap, re-clone away unpublished refs, remove packs, or rewrite
source refs to make verification pass.

After the source is delivered, the operator uses the normal deployment/restart
procedure to activate it. Workers do not deploy, restart the operator daemon,
migrate its database or mutate a live cache. There is no new live repair command.
An exhausted incident remains held for supervisor/operator handling; it is not
cleared by a deployment or by approval of the implementation.

Historical evidence for `crisp-crest-36` recorded 51 empty loose objects; the
operator reported that setting those objects aside and refetching produced clean
fsck. The evidence is in the operator vault under
`notes/evidence/retained-git-corruption-1791988496819898674/`. Neither these counts
nor that report establish current live health or the writer responsible for the
corruption.

Disposable Git/PostgreSQL acceptance cases live in
`tests/test_development_integration.py`. Detection and child lifetime regressions
remain in `tests/test_git_app_auth.py` and `tests/test_git_manager_async.py`.
