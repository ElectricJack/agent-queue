# Hosted attestation payloads

Recorded 2026-09-28 on the private fixture repository
`ElectricJack/aq-gh615-app-fixture-20260923` during the App-mode live proof
(spec §10; `docs/gates/app-mode-train-2026-09-28.md`). They are the vectors
`tests/test_hosted_attestation.py` replays through the stdlib verifier.

- `s4-attestation-check-runs.json` and `s8-contract-attestation-check-runs.json`:
  `GET /repositories/1384141153/commits/<sha>/check-runs?check_name=Agent Queue Integration Attestation&filter=all`
  on the promoted candidates `df8c833b…` (check set `fixture-v1`) and
  `cc79fa1d…` (`fixture-v2`, after the S8 rotation). Each holds the one
  attestation the daemon published through App 5075923.
- `s4-check-run-108871423828.json` and `s8-contract-check-run-108887702637.json`:
  `GET /repos/ElectricJack/aq-gh615-app-fixture-20260923/check-runs/<id>`, the
  CI check run each attestation names, which the verifier re-reads.
- `s6b-forged-check-runs.json`: the S6b negative control. A workflow on a
  scratch branch created a check run with the attestation's name using
  `GITHUB_TOKEN`, so GitHub attributes it to Actions (App 15368).
