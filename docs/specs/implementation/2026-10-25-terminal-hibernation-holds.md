# Retiring historical hibernation holds

The hibernation sleep-decision command rechecks task ownership under the graph
and component task locks. A terminal owner (`COMPLETED`, `FAILED`, `BLOCKED`)
retires its wait execution state through the same helper used by terminal source
events and deadlines, even if the wait already resolved or was cleared. The
continuation becomes `cancelled`, so subsequent lifecycle ticks do not dispatch
it again. Retirement preserves checkpoints, saved heads and committed outcomes;
it does not assert stop proof or release a claim or checkout.
An already cleared task wait also retires its own lingering continuation state,
without retiring a replacement wait or changing the current task.

A local operator or live project supervisor can use `aq wait cancel WAIT_ID` on
a resolved wait with a held continuation. If its ownership fence fails, the
command retires that wait's execution state, cancels only its continuation's
reservations and records `wait.orphan_cancelled`. The committed outcome and
result reference remain unchanged. The command needs no operational escalation
or source observation. Valid owners retain their resolved outcome and delivery
path; workers and supervisors of another project cannot retire orphan state.
