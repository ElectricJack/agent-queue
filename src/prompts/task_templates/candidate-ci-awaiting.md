

## Awaiting candidate CI — no failure is on record yet

{{identity}}{{cause}}Protocol:
1. Check the candidate CI status for the revision above before acting (read the run above with your CI read; `aq git ci-baseline-status` covers branch baselines, not this candidate).
2. While the candidate is still being built or the run is pending, missing, cancelled or inconclusive: push nothing at all to the batch branch, and close this stage pass-unchanged when CI turns green; the daemon publishes under its fence.
3. If and only if a required check on the exact candidate SHA fails: then repair, record the failed checks, and submit through aq integration resolve-candidate-member as usual.
A fix after a green result cannot be bound to this candidate; it would force a superseding revision instead. Ask the supervisor only if the candidate CI run for this exact SHA cannot be read at all.