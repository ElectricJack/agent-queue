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

Recorded 2026-09-28 through the App `agent-queue-train` (5075923), with an
installation token minted for the private fixture repository alone, by the
live proof's `prepare` step (`scripts/e2e-app-train.sh`, spec §10):

- `fixture-classic-protection.json`:
  `GET /repositories/1384141153/branches/main/protection`, the classic
  protection on `ElectricJack/aq-gh615-app-fixture-20260923` described in
  spec §2, which requires `fixture` from App 15368 (strictly up to date).

Recorded 2026-09-28 through the App's own installation token during the live
proof's S2 (spec §10), after `Train-only main` (ruleset `24108467`, the §8.1
shape) was applied to the fixture:

- `fixture-rules-main-attested-only.json`:
  `GET /repositories/1384141153/rules/branches/main?per_page=100`
- `fixture-ruleset-24108467-app-never.json` and
  `fixture-ruleset-24108467-app-always.json`:
  `GET /repositories/1384141153/rulesets/24108467`, first with no bypass actor,
  then with the App (`Integration` 5075923, `always`) as one. The App's token
  reads `current_user_can_bypass` for itself (`never`, then `always`), and
  GitHub returns no `bypass_actors` list to it: spec §8.3's assumption,
  confirmed live.
- `fixture-classic-protection-404.json`: the same classic-protection read after
  S1 removed it (HTTP 404, "Branch not protected"), with its status.
