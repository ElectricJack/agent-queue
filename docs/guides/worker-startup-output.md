# Worker startup and evidence output

Measured for azure-vault-92.5 on 2026-10-01, against source base
`c3f5a057ea327461eeaeb216868774376b7b64ed`.

AQ-owned workflow prose is compact. Prime still renders the complete profile
Role/Rules, project override, task/spec context, messages, claim identity and
continuation evidence. Applicable skills remain discoverable in the harness
catalogue; workers read their SKILL.md and authoritative project/directory
instructions before use. Shipped worker template maintenance notes now have a
separate heading, outside the agent-visible Role. Rules and capabilities are
unchanged. Pool completion prescribes one close/next-claim loop; development
completion now also retains heartbeat and durable findings instructions.

## Source inventory

UTF-8 bytes and chars/4 estimates below are source measurements, not provider
billing. Template files are subsets of rendered prime: do not add both together.

| Source | Before bytes | After bytes | Before estimated tokens | After estimated tokens |
|---|---:|---:|---:|---:|
| `AGENTS.md` | 14996 | 14996 | 3742 | 3742 |
| `profile.md` | 22996 | 22996 | 5572 | 5572 |
| `src/prime/templates/tool_guidance.md` | 3442 | 2429 | 858 | 607 |
| `src/prime/templates/emergent_work.md` | 3198 | 1274 | 797 | 318 |
| `src/prime/templates/completion_protocol_pool.md` | 1343 | 791 | 334 | 197 |
| `src/prime/templates/completion_protocol.md` | 7251 | 3427 | 1810 | 856 |
| `src/profiles/defaults/worker-claude/profile.md` | 7522 | 7547 | 1877 | 1883 |
| `src/profiles/defaults/worker-codex/profile.md` | 7507 | 7532 | 1873 | 1880 |
| Task bootstrap (`example-task`, `/tmp/example-worktree`) | 576 | 576 | 144 | 144 |
| Pool bootstrap (`agent-queue`, `standard-high-codex`) | 742 | 742 | 185 | 185 |
| Unknown-metric continuation guidance | 1135 | 1135 | 283 | 283 |

`AGENTS.md` and `profile.md` are retained verbatim. Project architecture and specs
are read when required by the project's own instructions; this change does not
replace them with an inferred summary. Prime L1/L2 memory slots are currently
empty even with memory enabled (`src/prime/sections.py`), so the audited MEMORY
cost is not a prime renderer cost. The operator audit recorded a 20,210-character
skill catalogue, 9,000-character MEMORY and 3,405-character superpowers injection
(estimated 5,052 / 2,250 / 851 tokens respectively). Their exact UTF-8 bytes were
not supplied by that audit. These harness/plugin sources remain unchanged;
no global plugin disabling, skill hiding or memory truncation is introduced.
The sampled 48,097-token first request is production evidence; this report does
not claim it has become a smaller measured request after deployment.

## Equivalent worker comparison

Each row joins the extracted shipped worker Role/Rules, tool guidance (including
identical unknown-metric continuation guidance), and the rendered completion
protocol with task id `example-task`. Task content, project overrides, messages,
AGENTS, harness system text, plugin instructions and tool schemas are excluded
and unchanged. This isolates the AQ-controlled change without double counting.
Both harness templates and both task/pool lifecycles use the same rendering path.
Development and review delivery policies remain distinct.

| Worker / lifecycle / delivery | Before bytes | After bytes | Before estimated tokens | After estimated tokens | Byte reduction |
|---|---:|---:|---:|---:|---:|
| claude/task/review | 20464 | 12957 | 5107 | 3238 | 36.7% |
| claude/task/development | 16769 | 12411 | 4184 | 3101 | 26.0% |
| claude/pool/review | 21811 | 13588 | 5442 | 3396 | 37.7% |
| claude/pool/development | 18116 | 13042 | 4520 | 3259 | 28.0% |
| codex/task/review | 20458 | 12957 | 5105 | 3238 | 36.7% |
| codex/task/development | 16763 | 12411 | 4183 | 3101 | 26.0% |
| codex/pool/review | 21805 | 13588 | 5440 | 3396 | 37.7% |
| codex/pool/development | 18110 | 13042 | 4518 | 3259 | 28.0% |

For example, retaining the 14,996-byte project AGENTS in both Codex pool/review
rows gives 36,801 → 28,584 bytes (22.3% reduction of those combined sources).
That is still a partial prompt comparison, not a provider quota estimate.

Shipped vault profiles/skills are write-if-absent: the role-heading improvement
applies to fresh installs or operator-reconciled templates, never overwrites
installed custom text. Existing installed profiles receive the shorter prime
templates when the updated renderer is deployed; their own Role/Rules remain
verbatim. Operators can inspect profiles.system_drift/skills.installed_drift;
workers must not modify installed copies or manage the daemon to deploy changes.

The held task's installed `standard-high-codex` profile has a 2,279-byte rendered
Role/Rules (569 estimated tokens). Reusing that exact installed text on both sides
of the pool/review comparison gives 18,675 → 11,195 bytes and 4,660 → 2,798 estimated
tokens (40.1% byte reduction); pool/development gives 14,980 → 10,649 bytes and
3,738 → 2,661 estimated tokens (28.9%). These installed-profile comparisons use the
same isolated guidance/completion measurement, without reseeding or editing the vault.

Reproduce the worker measurements in an isolated checkout of each revision:

```bash
python - <<'PYMEASURE'
from pathlib import Path
from src.prime.sections import (
    _extract_profile_prompt, build_tool_guidance_section,
    build_completion_protocol_section,
)
for harness in ('claude', 'codex'):
    profile = Path('src/profiles/defaults') / f'worker-{harness}' / 'profile.md'
    for lifecycle in ('task', 'pool'):
        for development in (False, True):
            parts = [
                _extract_profile_prompt(profile.read_text()),
                build_tool_guidance_section().body,
                build_completion_protocol_section(
                    'example-task', lifecycle=lifecycle, development=development,
                ).body,
            ]
            text = '\n\n'.join(parts)
            print(harness, lifecycle, development, len(text.encode()), len(text)//4)
PYMEASURE
```

## Saving and paging large evidence

Use `--brief` for ordinary entity summaries. For a large emit-based CLI result,
choose a new file in an existing private evidence directory:

```bash
mkdir -p .aq/evidence
aq git diff --save-output .aq/evidence/diff.json --json
python - <<'PYPAGE'
import json
from pathlib import Path
saved = json.loads(Path('.aq/evidence/diff.json').read_text())
print(saved['data'].keys())
# Select the relevant field, then page line ranges instead of reprinting it all.
lines = saved['data'].get('diff', '').splitlines()
print('\n'.join(lines[100:160]))
PYPAGE
```

The receipt names an absolute path, exact byte count, SHA-256 and data shape.
The new file is created with mode 0600 and exclusive creation; existing evidence
and leaf symlinks are refused. The complete unprojected versioned envelope,
including returned/total/truncated pagination, is saved as indented UTF-8 JSON.
`--brief` and AQ_JSON_LEGACY do not shrink saved evidence. Read individual keys
or line pages from it and retain its path in durable task comments/handoff.
No authorization scope, daemon state or original transcript is changed.

Errors, nonzero exit results, nested warnings/failures, gates, claim outcomes,
and instruction-bearing payloads keep their full visible data in addition to
the evidence pointer. Prime markdown and hook envelopes retain instructions.
Error-envelope handling keeps exact details even if saving itself fails. If saving
a returned success fails, its complete result is printed with output_save_error
and automatic_retry=false before exit 1; fixing output must not replay the command.
This conservative retention sometimes saves no visible tokens; preserving the
required next action takes precedence over receipt size. Save-output only
applies to commands using emit/error-envelope handling; streaming subprocess
output, local help, hooks and custom printer paths retain their own behavior.
Use shell redirection for other large success logs and explicitly read failures.

The synthetic 100-row Unicode log fixture saved 204,916 bytes and emitted a
240-byte receipt at `/tmp/azure-vault-92.5-output-example.json` (99.88% fewer visible
bytes) while preserving every row,
pagination and hash in the saved artifact. This measures CLI presentation,
not production model turns, provider tokens or quota recovery. Test assertions
cover receipt integrity, private/exclusive files, nested failures, instruction
preservation, JSON/human error paths, flag positions and unchanged default output.

## Verification

Focused prime/profile/envelope/global-option checks passed 204 tests. The final
related prime, CLI framework, profile inheritance/catalogue/capability and
selection-catalogue run passed 967 tests, with 8 existing conditional skips.
Both used `aq test` with the disposable PostgreSQL test service on port 5534;
the operator database and worker refusal sentinels were untouched. Ruff on all
changed Python paths and `git diff --check` passed. Generated artifacts were
regenerated; only CLI inventory and selection catalogue changed. No API model
or wire contract changed, and no full-suite run was performed.
