# Dry run: the installed routing policy

Recorded output of `PYTHONPATH=. python docs/superpowers/specs/2026-10-09-risk-aware-routing/dry-run.py` (2026-10-09), after
Jack's decisions in review `rev-wise-impact` (README, *Decisions*). The
approval-time run, which compared the pre-revision policy with the draft, is
this file at commit 8fecf5ef5.

installed policy sha256:42b499b0557a71d07b285ef0c4b913299b72fb795a21c490466d4ca3ed367f33

| Example | Prio | Risk | Asks | Route |
|---|---|---|---|---|
| fleet-ridge-45.1 recovery resumes on the task's own branch | 230 | very_high | narrow, risk, test_verified | `deep-low-codex` — raised from standard-high |
| fleet-ridge-45.2 delete branches on land | 230 | very_high | narrow, risk, test_verified | `standard-high-codex` |
| fleet-ridge-45.3 delete branches on abandon + audit table | 230 | very_high | narrow, risk, test_verified | `deep-low-codex` — raised from standard-high |
| fleet-ridge-45.4 hand-landing merges, never rebases | 230 | high | narrow, risk, test_verified | `standard-high-codex` |
| fleet-ridge-45.4 docs-only slice (guide text) | 100 | low | narrow, risk, test_verified | `standard-high-opencode` (lane narrow) |
| fleet-ridge-45.4 docs-only slice at the epic's priority | 230 | low | narrow, risk, test_verified | `standard-high-opencode` (lane narrow) |
| fleet-ridge-45.5 provenance refs off refs/heads | 230 | high | narrow, risk, test_verified | `standard-high-codex` |
| fleet-ridge-45.6 daily backstop branch sweep | 230 | very_high | narrow, risk, test_verified | `deep-low-codex` — raised from standard-high |
| fleet-ridge-45.7 one-time backlog branch cleanup | 230 | very_high | narrow, risk, test_verified | `deep-low-codex` — raised from fast-high |
| quilt-trader: order-size rounding fix, narrow + tested | 100 | very_high | narrow, risk, test_verified | `standard-high-codex` |
| quilt-trader: new position-sizing strategy | 100 | very_high | narrow, risk, test_verified | `deep-low-codex` |
| docs: fix a guide's broken links | 100 | low | narrow, risk, test_verified | `fast-low-opencode` (lane narrow) |
| UI: narrow, test-verified dashboard tweak | 100 | low | narrow, risk, test_verified | `standard-high-opencode` (lane narrow) |
| UI: the same tweak (local OpenCode busy) | 100 | low | narrow, risk, test_verified | `standard-high-codex` |
| same tweak in shared code (medium risk) | 100 | medium | narrow, risk, test_verified | `standard-high-codex` |
| trivial chore: bump a pinned version | 100 | low | narrow, risk, test_verified | `fast-low-opencode` (lane narrow) |
| trivial chore (local OpenCode busy) | 100 | low | narrow, risk, test_verified | `standard-high-opencode-zen` (lane narrow-hosted) |
| same chore at priority 200 | 200 | low | narrow, risk, test_verified | `fast-low-opencode` (lane narrow) |
