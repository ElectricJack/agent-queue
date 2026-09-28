# GitHub protection payloads

Fixtures for `tests/test_integration_protection.py` (App-mode integration train
spec §8.3).

Recorded 2026-09-28 from the public GitHub API without credentials:

- `agent-queue-rules-main.json`:
  `GET /repositories/1160639300/rules/branches/main?per_page=100`
- `agent-queue-ruleset-21960669.json` ("Basic Protection") and
  `agent-queue-ruleset-24002443.json` ("Train-only main"):
  `GET /repositories/1160639300/rulesets/{id}`

An anonymous read carries no `current_user_can_bypass`. The tests add the
value an App installation token would see, and they also use the recorded
payload as it is to show that the reader reports `unverifiable` when the field
is absent.

Built from GitHub's documented response shape, not recorded:

- `fixture-classic-protection.json`: the classic protection on
  `ElectricJack/aq-gh615-app-fixture-20260923` described in spec §2, which
  requires `fixture` from App 15368. That repository is private, and reading
  classic protection needs a credential.

The live proof (spec §10, S1-S6) replaces the built payloads with recordings
made through the App.
