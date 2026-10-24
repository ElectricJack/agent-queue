# Delivery watchdog observations

The completed-branch inventory used by the delivery watchdog recognizes work
already present on the default target even when hand landing changed its commit
identity. After a known negative ancestry result, equal source and target trees,
or a merge of the source that leaves the target unchanged (apart from declared
generated artifacts, following the existing Git content proof), removes the
branch from the owed inventory. Git failures remain unavailable observations.
This inventory does not authorize publication, branch deletion, or satisfy the
train's strict completion containment contract.

The watchdog does not observe Git, file takeover tasks, or raise incidents for
PAUSED or ARCHIVED projects. Existing pending projections become unknown with a
project lifecycle reason and their consecutive stuck count resets. Resuming a
project allows fresh observations without discarding completion age or history.

The owning project supervisor or global supervisor can withdraw a false delivery
incident through the existing revision-guarded control:

```sh
aq escalation update --escalation-id INCIDENT --expected-revision REVISION --state cancelled --terminal-outcome 'False alarm: work was hand landed'
```

On the next observation of that generation, the watchdog cancels its projection
and stops redriving or paging it, including across daemon restarts. This records
a supervisor decision, never a Git containment proof. A different completion
generation retains its own watchdog obligation. Other unanswered escalation
sources still require their existing human-answer or withdrawal protocol.
