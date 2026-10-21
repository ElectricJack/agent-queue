# Shared pool placement and train repair priority

Pool bounds remain fleet-wide. A worker stays bound to its project, and held
claims and launches in flight are never interrupted to rebalance the fleet.

For each available start, eligible projects with unserved active train repair
delegates precede ordinary demand and warm floors. Other starts honor warm
floors, then choose the project with the fewest live workers of that profile
among projects with unserved demand. Backlog size and project id break ties.
Recompute the ordering after every placement. A large backlog must not consume
every seat while another eligible project has no workers.

Repair urgency comes from the current active stage of an active or escalated
batch repair operation, bound to a repair delegate. Retired stages, completed
operations, and titles containing "repair" confer no priority. Count repair
demand with the same admission and routing predicates as ordinary pool demand.
When profiles compete for box-wide headroom, profiles with unserved repair
demand receive starts before ordinary profiles. Claims choose eligible repair
delegates before ordinary work, retaining the ordinary priority/affinity order.

At a saturated pool, idle workers above their warm floor may be retired after
the scale-down grace to fund an eligible project's smaller share, even if the
donor has ordinary queued work. Repair demand can also take idle ordinary
capacity. Protect capacity already serving repair demand. Normal sizing owns
replacement; never raise a cap or preempt a busy worker.

The stall sweep emits a distinct `train_repair_capacity_wait` finding for a
READY active train repair waiting for compatible worker capacity longer than
30 minutes. Include task, batch, operation, profile, age and scheduling reasons.
Dependency waits and other admission blockers do not qualify as capacity waits.
`aq task explain` also exposes this reason for the supervisor's timed patrol,
which prints `TRAIN-REPAIR-CAPACITY` instead of the generic `STUCK` line.
If all workers are busy, progress waits for task completion; the alarm exposes
this condition without interrupting claimed work.
