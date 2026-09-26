"""Smart test selection: which default-marker test modules a change needs.

A selection proposes, from a complete git change snapshot, the modules a
change must run: the mandatory set ``M`` from reviewed rules, the static
import closure ``S``, and the areas Jev does not confidently call unaffected.
Its inputs are reviewed artifacts under ``tests/`` (areas, rules, policy) plus
a catalogue generated from the tree, all digested so a record names exactly
what it was computed from.  ``M`` is never removed by any model answer, and an
incomplete snapshot, a global invalidator or an unmapped path makes it the
whole universe.  Nothing here runs pytest or decides policy: ``aq test`` runs
what a selection proposes, and only a promoted, measured policy lets Jev omit
a statically impacted module.  Spec: ``vault/projects/agent-queue/specs/
2026-09-24-smart-test-selection-2.md``.
"""
