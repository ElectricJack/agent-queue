# Matter Engine agentic command-line implementation plan

## Intent

Make the procedural editor inspectable and controllable by coding agents through its existing command registry and control transport. Start with precise object identity, selection and visual evidence, then add procedural source tracing and regeneration feedback. These are real AQ feature epics, not synthetic queue entries.

## Existing foundations

MatterEditor already owns one `SelectionSet` with entity/baked-root identity, app-thread command dispatch, GUI picking, camera control and screenshot completion. Extend these shared paths. Native acceptance must use the MSVC wrappers documented in Matter Engine CLAUDE.md.

Blender MCP provides useful examples of scene/object inspection, viewport screenshots and script execution ([upstream server](https://github.com/ahujasid/blender-mcp/blob/main/src/blender_mcp/server.py)). For Matter Engine, prioritize source provenance, declared parameters, deterministic regeneration and explicit revision evidence; keep normal code editing in source control.

## AQ task graph

Project: `matter-engine-cpp`. Epic `smart-dune` depends through its first child on completed aggregate `nimble-dune`. Every node carries self-contained implementation rules and acceptance criteria in AQ.

### nimble-dune: Epic: Agent-facing scene inspection and precise selection

| Task | Deliverable | Prerequisites |
|---|---|---|
| `nimble-dune.1` | Define discoverable agent command and response contracts | None |
| `nimble-dune.2` | List and inspect scene objects with typed stable IDs | nimble-dune.1 |
| `nimble-dune.3` | Select, deselect and list selections by object ID | nimble-dune.2 |
| `nimble-dune.4` | Ray-cast viewport coordinates through the existing picker | nimble-dune.3 |
| `nimble-dune.5` | Capture selection-aware viewport evidence and focus selected objects | nimble-dune.4 |
| `nimble-dune.6` | Verify native Windows agent selection workflow and document the CLI | nimble-dune.5 |

### smart-dune: Epic: Procedural authoring feedback and agent automation

| Task | Deliverable | Prerequisites |
|---|---|---|
| `smart-dune.1` | Trace inspected objects to procedural source and generation inputs | nimble-dune |
| `smart-dune.2` | Expose bounded regeneration job control and diagnostics | smart-dune.1 |
| `smart-dune.3` | Validate and apply procedural parameter changes with preview | smart-dune.2 |
| `smart-dune.4` | Compare generated scene revisions and query spatial results | smart-dune.3 |
| `smart-dune.5` | Provide a reusable agent CLI client and bounded command batches | smart-dune.4 |
| `smart-dune.6` | Run procedural agent workflow and AQ cross-epic acceptance | smart-dune.5 |

## Verification and execution

The committed graph JSON files are the detailed implementation plan. Both were validated with `aq task create --dry-run`, created through the real CLI, and must be checked against the persisted hierarchy/dependency graph.

Matter Engine was ACTIVE with no tasks and legacy integration disabled. Scheduling is temporarily PAUSED while its designated repository and new feature-branch/CI policy are configured. This is an operator-owned preparation step, not a completed acceptance result. Resume once those requirements are verified; do not run this workload through legacy direct-main delivery.

Native editor tests, canonical build/package checks, deterministic fixtures and command transcripts prove the product behavior. AQ assignment, dependency availability, per-child reviews/receipts, aggregate verification and exact delivery evidence prove the orchestration behavior. Separate the two.

Throughput acceptance additionally needs a bounded orchestration workload representing hundreds of tasks/day, with reported concurrency, latency, throughput and recovery behavior. No current result proves that scale.
