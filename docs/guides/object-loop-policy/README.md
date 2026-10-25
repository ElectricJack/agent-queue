# Simplified object-loop policy

[source.md](source.md) is the exact replacement source approved in
`rev-clear-apex`, revision 2, on 2026-10-24 by the local operator. The approved
review's content SHA-256 is
`2c6ebd4cc4aa90b8cd1d3c18f53c84702a6fe661b2524f67625be57c44f872eb`.
The decision is `ask-decision-1c8dbf117ea04c6e8881e14d5efd514b`.

The source reduces the policy from 1,722 to 863 whitespace-separated words,
removes personal names, and leaves bindings, field mappings and graph structure
to the compiler. It retains the admission, approval, comparison, budget, retry,
settlement and reporting requirements. This completes the wording requested in
`cmt-0a7dd98b2341`; it introduces no amendment to the approved requirements.

## Compilation boundary

The approval covers wording only. This directory contains an authoring source,
not an importable V2 bundle. The current compiler's identifier inventory and
source-policy alignment checks require literal paths and predicates that the
shorter text deliberately omits. Pairing it with the existing semantic graph
does not produce an activatable artifact.

Executable adoption therefore still requires compiler resolution, validation of
the resulting graph against this policy, and separate approval of the compiled
artifact's exact hash followed by an operator activation decision. Preserve all
policy limits and authority checks during that work. The earlier artifact and
test traces validate their recorded source, not this wording.

The [object-loop handoff](../object-loop.md) describes the existing inactive
bundle, formulas and operating prerequisites. The original approved review and
its vault mirror remain canonical and unchanged.
