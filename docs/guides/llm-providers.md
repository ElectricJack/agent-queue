# Setting up LLM providers

Credentials, model choices, quota reporting and prices — everything an operator
configures before AQ can think or spend on their behalf.

This is the task-shaped companion to
[Providers, models and token accounting](../concepts/providers.md). Read the
first two sections of that page if the words *harness*, *direct LLM path* and
*intelligence class* are new; this guide assumes them.

## Decide what you actually need

There are two independent things to set up, and many installs only need the
first:

| You want… | You need |
|---|---|
| Agents to write code | A coding-agent CLI installed and logged in: `claude`, `codex` or `gemini`. AQ does not hold these credentials — the CLI does. |
| Playbooks with `llm` steps, plugin `invoke_llm`, vault summaries | An `llm:` block with a credential the daemon itself can use. |
| Quota cards on the dashboard | Nothing for Codex. For Claude, the `provider-usage-probe` playbook activated. |
| Costs in dollars | A `pricing:` table. It ships empty. |

All configuration below lives in `~/.agent-queue/config.yaml`. `providers:` and
`pricing:` are hot-reloadable; **`llm:` requires a daemon restart**
(`HOT_RELOADABLE_SECTIONS` / `RESTART_REQUIRED_SECTIONS` in
[`src/config.py`](../../src/config.py)).

> **Warning.** Restarting the daemon is an operator action taken outside a
> worktree slot. If you are an agent working in a slot, do not run `aq start`,
> `aq stop` or any migration — see [migrations](migrations.md).

## Set up the coding-agent CLIs

Install each CLI you intend to use and log it in *as yourself*, the way you
would to use it by hand. AQ starts these binaries in a terminal and inherits
whatever authentication they already have.

Two consequences worth knowing before you debug anything:

* **AQ cannot fix a login.** If `claude` would prompt you for credentials in
  your own shell, a session started by AQ hits the same prompt.
* **Quota is shared with you.** A `codex` window you run yourself draws on the
  same account the fleet does, and the quota cards will show it.

Which CLI a given agent uses is the profile's `harness` field, covered in
[the harness reference](../reference/harnesses.md) — not something you set here.

## Give the daemon a provider

This is the `llm:` block, and it applies only to the direct LLM path: playbook
`llm` steps, plugin `invoke_llm`, reference-stub enrichment and
`aq vault rebuild-index --with-summaries`. It has no effect on coding-agent
sessions.

```yaml
llm:
  provider: anthropic        # anthropic | google | openai
  model: ""                  # explicit id; empty = let the intelligence class decide
  api_key: ""                # optional — the environment is usually better
  base_url: ""               # openai only: an OpenAI-compatible endpoint
  max_tokens: 4096
  default_class: standard-medium   # class used when a call names none
```

Those are the shipped defaults except `default_class`, which ships empty
([`LLMConfig`](../../src/config.py)). Legacy configuration that still spells
this block `chat_provider:` is read with a deprecation warning, and its old
provider ids are mapped (`gemini` → `google`, `ollama` → `openai`).

### Credentials, per provider

Prefer the environment (or a `.env` beside the config) over `api_key:` in the
file — the config file is read by more things than the daemon.

**Anthropic** ([`src/llm/providers/anthropic.py`](../../src/llm/providers/anthropic.py))
tries four authentication methods in a fixed order and takes the first that
matches, so the same configuration works on a laptop and in a cloud deployment:

1. **Vertex AI** — `GOOGLE_CLOUD_PROJECT` or `ANTHROPIC_VERTEX_PROJECT_ID`
   (region from `GOOGLE_CLOUD_LOCATION` / `CLOUD_ML_REGION`, else `us-east5`).
2. **Bedrock** — `AWS_REGION` or `AWS_DEFAULT_REGION`.
3. **API key** — `api_key:` in the config, else `ANTHROPIC_API_KEY`.
4. **Claude Code OAuth** — the token in `~/.claude/.credentials.json`.

Because the order is fixed, a stray `AWS_REGION` in the daemon's environment
silently routes to Bedrock. If the provider resolves somewhere unexpected, look
there first.

**Google** ([`src/llm/providers/google.py`](../../src/llm/providers/google.py)):
set `GOOGLE_GENAI_USE_VERTEXAI=true` for Vertex (the SDK then finds application
default credentials itself), otherwise `api_key:`, `GEMINI_API_KEY` or
`GOOGLE_API_KEY`.

**OpenAI and OpenAI-compatible endpoints**
([`src/llm/providers/openai.py`](../../src/llm/providers/openai.py)): `api_key:`
or `OPENAI_API_KEY`. Any `base_url` that is not `api.openai.com` is treated as
a *local* endpoint — which is how a self-hosted server such as Ollama is used:

```yaml
llm:
  provider: openai
  base_url: http://localhost:11434/v1
  model: qwen3.5:35b
```

Local mode changes three behaviours: a placeholder key is supplied so the SDK
does not refuse to start, a keep-alive hint is sent with each request, and
`is_model_loaded()` probes `/api/ps` to see whether the model is resident —
failing *open*, so a probe that cannot answer never blocks a caller.
Configuration validation rejects `provider: openai` with neither a `base_url`
nor any key.

### Check that it resolved

```bash
python3 - <<'PY'
import os
from src.config import LLMConfig
from src.intelligence_classes import load_intelligence_classes
from src.llm.client import LLMClient
from src.llm.spec import LLMCallSpec

classes = load_intelligence_classes(os.path.expanduser("~/.agent-queue"))
client = LLMClient(LLMConfig(provider="anthropic"), classes_loader=lambda: classes)
spec = LLMCallSpec(intelligence_class="fast-low")
print("model     :", client.resolve(spec).model)
print("configured:", client.is_configured(spec))
PY
```

```text
model     : claude-sonnet-5
configured: False
```

That is a real run on a checkout with no vendor SDK installed. `configured` is
`False` whenever no credential resolved **or** the SDK is missing — the client
answers *without* making a call, so the check costs nothing. Substitute your own
`provider=` to check another vendor.

The SDKs are optional extras, so a daemon that only runs coding-agent sessions
need not carry any of them ([`pyproject.toml`](../../pyproject.toml)):

```bash
pip install -e ".[anthropic]"     # or [google], [openai], or [llm] for all three
```

## Choose which model each tier means

You rarely name a model directly. Callers name an **intelligence class**, and
the class maps to one model per provider. The twelve shipped classes are seeded
into `vault/intelligence-classes/` on first start
([`src/prompts/default_intelligence_classes/`](../../src/prompts/default_intelligence_classes/));
the vault copy wins from then on, and your edits survive restarts.

To point a tier at a different model, edit its file:

```bash
$EDITOR ~/.agent-queue/vault/intelligence-classes/standard-high.md
```

Keep the frontmatter, and edit only the model ids and reasoning fields inside
the single fenced `json` block. Each provider gets its own slice; `codex` is an
optional fourth slice that overrides `openai` for the Codex CLI only.

A saved file is picked up by the vault watcher without a restart. Confirm with:

```bash
aq system list-intelligence-classes     # shows `loaded` per class
aq system reload-config                 # forces a rescan
aq doctor --check intelligence_classes.parse
```

A malformed file keeps the *previous* entry in memory rather than dropping the
class, so a bad edit degrades to "the change did not take" and the doctor check
names the file.

> **One exception to "the vault copy wins".** When a shipped class's slice is
> still *byte-identical to a previous release's* default, loading it swaps in
> the current bundled model for that provider — each provider independently,
> in memory, without rewriting your file. A slice you edited, an explicit
> `codex` entry, a class id that never shipped, and any class the editor marked
> `customized` are all left exactly alone
> ([`src/intelligence_classes/__init__.py`](../../src/intelligence_classes/__init__.py),
> `_upgrade_legacy_provider_defaults`). So an untouched tier follows upgrades,
> and a tier you chose stays chosen.

> **Do not paste model ids from documentation.** Read the ones your vault
> actually has, with the snippet in
> [the concept page](../concepts/providers.md#example-see-which-models-your-classes-resolve-to).

## Turn on the Claude quota probe

Codex reports its remaining quota passively, on transcript lines AQ already
reads — there is nothing to enable. Claude publishes no local equivalent, so
the only way to learn what the subscription has left is to ask the CLI, on a
timer, from a playbook.

The daemon reconciles the reviewed `provider-usage-probe` bundle as a required
system activation at startup, so a configured Claude account starts receiving
readings without an operator action. Set `usage_probe_enabled: false` to opt
out; that disables subprocess launches while leaving the last verified reading
honestly visible as it becomes stale. Its source is
[`src/prompts/default_playbooks/provider-usage-probe.md`](../../src/prompts/default_playbooks/provider-usage-probe.md),
seeded to `~/.agent-queue/vault/system/playbooks/`, and its reviewed bundle is
shipped under `src/prompts/reviewed_playbooks/provider-usage-probe/`.

The relevant settings, with their shipped defaults:

```yaml
providers:
  claude:
    usage_probe_enabled: true     # false is a decision, and reports as healthy
    binary: claude                # point at a shim if the real CLI is not first on PATH
    stale_after_seconds: 1500     # 25 min: twice the 10-minute cadence, plus slack
  codex_stale_after_seconds: 14400   # 4h: a Codex reading only moves while Codex runs
```

`stale_after_seconds` is one horizon read by three surfaces — the API, the
dashboard card and the doctor check — so they cannot disagree about what a
given card means.

The probe costs nothing: a live `claude -p "/usage" --output-format json`
reports `num_turns: 0` and `total_cost_usd: 0`, so it bills none of the quota
it reports. It never passes `--bare`, which would read an API-key account
instead of the one your sessions use.

### Confirm it is working

```bash
aq doctor --check providers.claude_usage
curl -s http://localhost:8081/api/providers/usage
```

The doctor check is report-only on purpose. There is no `--fix`: a stalled
timer needs a playbook activated and a moved CLI wording needs a human editing
a regex, and a `--fix` that "helpfully" ran one probe would paper over exactly
the stalled timer the check exists to surface.

## Give the cost rollup some prices

`aq costs` rolls the token ledger up into dollars. The price table **ships
empty**, which means every token is reported as unpriced until you fill it in —
deliberately, so that no stale rate ever ships in the repository.

```yaml
pricing:
  - model: "claude-opus-*"
    input_per_mtok: 0.0        # USD per million input tokens — use your own rates
    output_per_mtok: 0.0
  - model: "gpt-5.6-*"
    input_per_mtok: 0.0
    output_per_mtok: 0.0
```

Entries are `fnmatch` globs matched **in order**, first match wins, so put
specific patterns above general ones. Rates are USD per million tokens
([`ModelPricing`](../../src/config.py)). Get the numbers from your provider's
current price list — AQ has no opinion about what a model costs, and does not
ship one that could go stale.

Two rules decide whether a ledger row is priced at all
([`src/commands/ops_commands.py`](../../src/commands/ops_commands.py)):

1. the row carries a **model** that matches an entry, and
2. the row carries an **input/output split**.

Anything else is counted in `unpriced_tokens` with a null cost. Cache reads and
cache writes are recorded separately and are not priced by this rollup, so a
cache-heavy fleet will show a large unpriced figure — that is honesty, not a
bug.

```bash
aq costs --group-by profile --since 7d
aq costs --group-by day --json
```

## Watch what is being spent

| Surface | Shows | Comes from |
|---|---|---|
| Dashboard → Metrics, token charts | Tokens per minute, split by model, with an `unattributed` residue | `token_ledger` via `GET /api/metrics/series` |
| Dashboard → Metrics, provider cards | The provider's own remaining-quota percentages, greyed when stale | `GET /api/providers/usage` |
| `aq costs` | Priced rollup by project, profile or day | `token_ledger` + `pricing:` |
| `logs/llm/<date>/llm.jsonl` | Every direct call: prompt, response, latency, **estimated** tokens | `LLMLogger` |
| `logs/llm/<date>/prompt_analytics.jsonl` | Hourly per-caller/model aggregates | `LLMLogger`, flushed hourly |

```bash
curl -s http://localhost:8081/api/metrics/series | python3 -c "
import json,sys; print(json.dumps(json.load(sys.stdin)['samples'][-1]['tokens'], indent=2))"
```

```text
{
  "input_per_min": 112.4,
  "output_per_min": 21086.2,
  "cache_read_per_min": 8802804.0,
  "cache_write_per_min": 100441.6,
  "total_per_min": 8924444.2,
  "unattributed_per_min": 0.0,
  …
  "by_model": {
    "claude-opus-5": {
      "input_per_min": 112.4,
      "output_per_min": 21086.2,
      …
    }
  }
}
```

Everything in `llm.jsonl` marked `_est` is a characters-÷-4 estimate of a
*direct* call. It is there to compare prompts with each other, never to bill
anyone. Only provider-reported counts reach the ledger.

Direct-path logging is controlled by:

```yaml
llm_logging:
  enabled: true
  retention_days: 30
```

Date directories older than the retention window are removed on an hourly
sweep, along with the analytics flush.

## When something is wrong

| Symptom | Cause | Fix |
|---|---|---|
| Playbook `llm` steps return `unavailable` | No credential resolved for `llm.provider`, or the vendor SDK is not installed | Run the [resolution check](#check-that-it-resolved); install the SDK extra; restart after editing `llm:` |
| Steps return `budget_exceeded` with no tokens spent | The step sets `max_total_tokens` and the adapter does not report usage | Use a reporting provider, or drop `max_total_tokens` |
| A class edit did nothing | The file failed to parse, so the previous entry is still in memory | `aq doctor --check intelligence_classes.parse`, then `aq system reload-config` |
| Requests go to the wrong vendor | Anthropic's fixed credential order matched something in the environment (Vertex, then Bedrock, then key, then OAuth) | Unset the stray variable, or set the one you want higher in the order |
| The Claude quota card never appears | The CLI is missing, the account is API-key billed, or the probe was disabled | `aq doctor --check providers.claude_usage` names which |
| The Claude card froze | The `/usage` wording moved and the parser reads nothing | The doctor check says so; a human updates the regex in [`src/providers/claude_usage.py`](../../src/providers/claude_usage.py) |
| A Codex card is hours old | No Codex session has run since | Nothing. The card says `stale` because it is, and that is the honest state |
| `aq costs` totals $0.00 | `pricing:` is empty, which is the shipped default | [Add your rates](#give-the-cost-rollup-some-prices) |
| A local endpoint answers slowly on the first call | The model is not resident; keep-alive expired | Expected — `is_model_loaded()` reports it and fails open rather than blocking |

## Related pages

* [Providers, models and token accounting](../concepts/providers.md) — the
  concepts behind every setting here.
* [Usage accounting reference](../reference/usage-accounting.md) — exhaustive
  field, table and outcome reference.
* [Harness reference](../reference/harnesses.md) — the other half of provider
  configuration: which CLI an agent runs.
* [Sessions](../concepts/sessions.md) — what happens to a task when a session
  hits a rate limit.
* [Migrations](migrations.md) — why a worker never restarts the daemon or
  touches the operator's database.

## Source and tests

[`src/llm/`](../../src/llm/), [`src/providers/`](../../src/providers/),
[`src/config.py`](../../src/config.py) (`LLMConfig`, `ProvidersConfig`,
`PricingConfig`), [`src/doctor/provider_checks.py`](../../src/doctor/provider_checks.py).

```bash
aq test tests/llm tests/test_provider_doctor.py tests/test_provider_usage_probe.py \
    tests/test_claude_usage_parser.py
```
