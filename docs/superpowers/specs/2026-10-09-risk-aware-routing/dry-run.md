# Dry run: shipped vs draft routing policy

Recorded output of `PYTHONPATH=. python docs/superpowers/specs/2026-10-09-risk-aware-routing/dry-run.py` (2026-10-09).

shipped policy sha256:af27b036017f783c1784e7e11c1c3800fa9ab971fdd9db241e9a0a619cd4f63e  
draft policy sha256:630789d780709a8923524c6a786c6bba6c4ff7b9378470d268c5dc8cd6283167

| Example | Prio | Risk | Shipped route | Draft asks | Draft route |
|---|---|---|---|---|---|
| fleet-ridge-45.1 recovery resumes on the task's own branch | 230 | very_high | `standard-high-codex` | narrow, risk, test_verified | `deep-low-codex` — raised from standard-high |
| fleet-ridge-45.2 delete branches on land | 230 | very_high | `standard-high-opencode-zen` (lane narrow-hosted) | narrow, risk, test_verified | `standard-high-codex` |
| fleet-ridge-45.3 delete branches on abandon + audit table | 230 | very_high | `standard-high-codex` | narrow, risk, test_verified | `deep-low-codex` — raised from standard-high |
| fleet-ridge-45.4 hand-landing merges, never rebases | 230 | high | `standard-high-opencode-zen` (lane narrow-hosted) | narrow, risk, test_verified | `standard-high-codex` |
| fleet-ridge-45.4 docs-only slice (guide text) | 100 | low | `standard-high-opencode` (lane narrow) | narrow, risk, test_verified | `standard-high-opencode` (lane narrow) |
| fleet-ridge-45.4 docs-only slice at the epic's priority | 230 | low | `standard-high-opencode-zen` (lane narrow-hosted) | narrow, risk, test_verified | `standard-high-codex` |
| fleet-ridge-45.5 provenance refs off refs/heads | 230 | high | `standard-high-opencode-zen` (lane narrow-hosted) | narrow, risk, test_verified | `standard-high-codex` |
| fleet-ridge-45.6 daily backstop branch sweep | 230 | very_high | `standard-high-codex` | narrow, risk, test_verified | `deep-low-codex` — raised from standard-high |
| fleet-ridge-45.7 one-time backlog branch cleanup | 230 | very_high | `fast-high-codex` | narrow, risk, test_verified | `deep-low-codex` — raised from fast-high |
| quilt-trader: order-size rounding fix, narrow + tested | 100 | very_high | `standard-high-opencode` (lane narrow) | narrow, risk, test_verified | `standard-high-codex` |
| quilt-trader: new position-sizing strategy | 100 | very_high | `deep-low-codex` | narrow, risk, test_verified | `deep-low-codex` |
| docs: fix a guide's broken links | 100 | low | `fast-low-opencode` (lane narrow) | narrow, risk, test_verified | `fast-low-opencode` (lane narrow) |
| UI: narrow, test-verified dashboard tweak | 100 | low | `standard-high-opencode` (lane narrow) | narrow, risk, test_verified | `standard-high-opencode` (lane narrow) |
| UI: the same tweak (local OpenCode busy) | 100 | low | `standard-high-opencode-zen` (lane narrow-hosted) | narrow, risk, test_verified | `standard-high-codex` |
| same tweak in shared code (medium risk) | 100 | medium | `standard-high-opencode` (lane narrow) | narrow, risk, test_verified | `standard-high-codex` |
| trivial chore: bump a pinned version | 100 | low | `fast-low-opencode` (lane narrow) | narrow, risk, test_verified | `fast-low-opencode` (lane narrow) |
| trivial chore (local OpenCode busy) | 100 | low | `fast-high-codex` | narrow, risk, test_verified | `standard-high-opencode-zen` (lane narrow-hosted) |
| same chore at priority 200 | 200 | low | `fast-high-codex` | narrow, risk, test_verified | `fast-high-codex` |
