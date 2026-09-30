# Reviews pull request inbox

The Reviews tab projects task-linked GitHub pull requests into a single
PostgreSQL snapshot. The API reads that row and reports its age; it does no
GitHub I/O on GET. A daemon background task refreshes every 60 seconds. It
groups task PR links by repository, binds each repository once, pages its open
PR list, and matches those open PRs to canonical task URLs. A successful pass
atomically replaces the snapshot. A failed pass leaves the previous snapshot
and its older timestamp in place. Closed and merged PRs disappear from the
open list and are never fetched individually.

For each open row the refresher stores the PR head SHA, the latest human
review decision at that SHA, check-run status at that SHA, and, for train
projects, the integration batch lifecycle. A review by a bot or for an older
head cannot decide the badge. A user approval is not proof of train admission
until the train's own review poller records its evidence.

The Approve action is an operator write through CommandHandler. It accepts a
task ID and the displayed 40-digit head SHA, requires that row in the
snapshot, checks the live PR head and open state, and submits GitHub's
`APPROVE` review with `commit_id` set to that SHA. It uses the host's saved
`gh` user login in a separate credential context that strips ambient token
overrides, so the review has `user.type == User`; it never uses the daemon's
GitHub App identity. The response must confirm a human review at that commit.
Afterward the API refreshes that row and requests an integration flush for a
train project.

The dashboard proxy distinguishes its real peer before relaying requests.
Loopback viewers and configured trusted origins reached over a Tailscale
address may see and use Approve. Other viewers receive no button, and their
POST is refused at the edge. The proxy replaces any inbound viewer assertion;
the Vite development proxy replaces it too and grants only loopback peers.
The daemon also requires local operator scope, a loopback proxy peer, and a
loopback Host for direct requests without a proxy verdict.

The `pull_request_inbox_snapshot` table arrives in revision 44. Deployments
must apply that revision before the new daemon starts and rebuild the
dashboard bundle after the API client and UI change.
