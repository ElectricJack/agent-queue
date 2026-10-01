# Claude streaming usage and production-ledger reconciliation

Task: `azure-vault-92.1`. Frozen UTC window:
2026-09-30 17:42:08.763758 through 2026-10-01 17:42:08.763758.

The duplication hypothesis is confirmed in the production ledger. All 10,886
Claude ledger rows in the window match transcript content UUIDs, with exact
agreement in all four token categories. They cover 5,781 API calls. Each call
has complete transcript UUID coverage; none crosses the window boundary.

| Token category | Recorded ledger | One maximum per API call | Excess |
|---|---:|---:|---:|
| Uncached input | 21,772 | 11,562 | 10,210 |
| Output | 7,526,078 | 3,484,271 | 4,041,807 |
| Cache reads | 1,885,404,007 | 993,794,418 | 891,609,589 |
| Cache writes | 21,952,066 | 11,211,640 | 10,740,426 |
| Total token events | 1,914,903,923 | 1,008,501,891 | **906,402,032** |

This measures inflated accounting. It does not measure avoidable model work,
restore provider quota, or imply a subscription quota percentage. The original
audit's 2,123,951,462 raw transcript events and 1,124,524,944 globally deduplicated
events include 6,585 calls, including subagents and other calls absent from this
ledger sample. Their difference is not the production-ledger correction amount.
The token-audit calendar-day aggregate also uses a different window and includes
other providers, so it is not the denominator of this comparison.

## Evidence and reproducible dry run

The production export used `default_transaction_read_only=on` and a PostgreSQL
READ ONLY transaction containing SELECTs only. No database initialization,
migration, correction, daemon operation or configuration change was performed.
Worker database-scope environment variables were left intact. Original operator
audit files remain unchanged.

Persistent evidence directory:
`/home/jkern/.agent-queue/operator-checks/usage-audit-20261001/worker-reconciliation/`.
`ledger-export.json` preserves all 14,169 ledger rows from the window and session
metadata. `report-with-attribution.json` preserves originals, hashes, transcript
window fingerprints and 3,962 proposed compensating corrections. The preliminary
`report.json` also remains preserved.

Canonical JSON SHA-256 fingerprints:

- Original `usage-corrected.json`:
  `0a744f19fb26820796175197130821a58960c07c1ddb51de481cdf26c1118a65`.
- Frozen `ledger-export.json`:
  `e84798f460350d9031db1efcd72a171506d7c4a0a843831a3a5b9b321fd99b5e`.

From the checkout, reproduce with a fresh output filename:

```bash
python scripts/reconcile-claude-usage.py \
  --audit /home/jkern/.agent-queue/operator-checks/usage-audit-20261001/usage-corrected.json \
  --ledger-export /home/jkern/.agent-queue/operator-checks/usage-audit-20261001/worker-reconciliation/ledger-export.json \
  --output /tmp/claude-usage-reconciliation.json
```

The tool has no database access or apply mode and refuses to overwrite output.
It excludes missing, ambiguous or mismatched UUID evidence, incomplete category
splits, incomplete call coverage and calls crossing the frozen window. Proposed
signed deltas retain original project, task, session, attempt, agent and model
attribution. Correction and entry IDs are deterministic; each adjustment carries
its inverse. A future application command must atomically verify original hashes
and unique IDs and append compensating rows. No historical correction was applied.

## Ingest behavior and rollout

Before: two transcript content UUIDs carrying one API message ID and identical
usage each create a full ledger charge. Reusing a content UUID for later final
usage drops the update, and a failed ledger write can still consume its bytes.

After: displayed content retains its UUID. Accounting uses the Claude provider,
transcript conversation path and API message ID, falling back to the content UUID
when an API ID is absent. A row lock protects durable per-call maxima; ledger rows
append only positive category increases in the same transaction. Restarts,
replay and concurrent readers preserve the total, including partial-line/final
updates. Later increases retain the first usage row's attribution. Accounting
failures leave the byte checkpoint retryable.

Consumed transcript UUIDs seed maxima from existing legacy ledger rows during
adoption. Existing inflated rows remain intact; seeding only prevents new copies.
Historical correction proposals and forward ingestion are separate operations.

Revision `a00000000055` adds the usage-progress table and legacy-call lookup index.
After normal integration delivers the change, the operator must apply the schema
upgrade before the updated daemon ingests usage. Workers do not migrate the
operator database. Downgrading removes progress state and reintroduces the old
accounting behavior; ledger audit rows survive.

Regression coverage includes duplicate blocks, distinct calls/conversations,
reused content UUIDs with changed counters, cache category preservation, missing
API IDs, partial lines, restart and checkpoint loss, old-row adoption, concurrent
readers, transaction rollback, idempotent migration and conservative correction
proposals with deterministic reversal evidence.
