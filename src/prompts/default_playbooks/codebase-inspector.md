---
id: codebase-inspector
triggers:
  - timer.4h
scope: system
---

# Codebase Inspector

Inspect a random section of the codebase for quality issues,
security risks, and documentation gaps. Follow weighted selection:
source (40%), specs (20%), tests (15%), config (10%), recent
changes (15%). Check inspection history to avoid re-inspecting
the same files. Only report concrete, actionable findings.

If the system health check recently flagged a related issue,
consolidate into one task rather than creating duplicates.
