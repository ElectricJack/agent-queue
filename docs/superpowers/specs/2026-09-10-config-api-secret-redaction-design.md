# Config API secret redaction — design

**Task:** `fresh-meadow` (found by website capture task `vivid-falcon.12`)
**Date:** 2026-09-10
**Status:** implemented

## 1. Problem

`_cmd_get_config` (`src/commands/system_commands.py`) returns
`read_raw_config(path)` unchanged. `read_raw_config` deliberately does *not*
resolve `${ENV_VAR}` references — that is what makes the Config editor a safe
round trip for env-backed values. It says nothing about credentials an
operator wrote **literally**:

```yaml
database:
  url: postgresql+asyncpg://aq:hunter2@db.internal:5432/agent_queue
discord:
  bot_token: xoxb-000000-literal
```

Those literals reach every reader of the Config API — the dashboard's System →
Config page, `aq system config get`, the MCP mirror, and any screenshot of
either. `GetConfigResponse.config` is `dict[str, Any]`, so the schema does not
constrain it. Env references are not protection: they only help the operators
who used them.

## 2. Constraint that shapes the fix

`_cmd_update_config` replaces a whole top-level section. A naive read-side
redaction therefore *destroys* configuration: the dashboard loads
`discord` with `bot_token: "<redacted>"`, the operator edits `guild_id`,
saves, and the placeholder is written over the real token.

So redaction has to come with an inverse on the write path.

## 3. Design

New leaf module `src/config_secrets.py` — pure, no I/O, no daemon state:

| Function | Role |
| --- | --- |
| `redact_config(raw)` → `(redacted, paths)` | read side: replace literal credentials with `SECRET_PLACEHOLDER`, report every dotted path touched |
| `restore_section_secrets(incoming, stored)` → `(restored, unresolved)` | write side: splice the stored literal back wherever the client sent a placeholder; report placeholders that have no stored counterpart |

`SECRET_PLACEHOLDER = "__aq_redacted__"`.

### 3.1 What counts as a literal secret

1. **Secret-shaped key** (`api_key`, `bot_token`, `client_secret`, `password`,
   `private_key`, `credential`, `dsn`, …) whose value is a **non-empty
   string**. Numeric and boolean knobs are never redacted, which is what keeps
   `metrics.token_window_seconds`, `llm.max_tokens`, `api_auth.token_ttl_hours`
   and `global_token_budget_daily` readable. A suffix exemption list
   (`_path`, `_file`, `_dir`, `_seconds`, `_hours`, `_budget`, `_ids`,
   `_users`, …) keeps `integration.github_app.private_key_path` and
   `discord.authorized_users` verbatim — a path is not a credential.
2. **Secret-shaped value**, whatever the key: `sk-…`, `sk-ant-…`, `ghp_…`,
   `github_pat_…`, `xoxb-…`, `AKIA…`, `AIza…`, a PEM `PRIVATE KEY` block.
3. **URL/DSN userinfo password** — `scheme://user:secret@host/db` becomes
   `scheme://user:__aq_redacted__@host/db`. Only the password component is
   replaced, so the host, port, database and user stay visible: that is what
   makes `database.url` diagnosable from the UI at all.

`${ENV_VAR}` references are left byte-identical in all three cases (a string
containing a reference is a template, not a literal), so the existing
env-reference round trip and the `env_var_references` panel are unchanged.

### 3.2 Restoring on save

`restore_section_secrets` walks the incoming section and the stored section in
parallel by path (dict keys, list indexes):

* value **exactly** `__aq_redacted__` → the stored string at that path;
* a DSN whose **password** is `__aq_redacted__` → the stored DSN's password
  spliced in, everything else the client sent kept;
* anything else → passed through verbatim.

That last line is the deliberate-credential-change path: a client that means
to rotate a credential sends the new literal and it is written. Only an exact
placeholder is ever restored, so a real change is never swallowed.

A placeholder with no stored counterpart (a renamed key, a new field, a
hand-pasted `__aq_redacted__`) is **not** guessed. It is returned in
`unresolved` and `_cmd_update_config` refuses the write with
`validation_errors` naming the paths, leaving the file byte-identical. The
failure mode is "your save was refused", never "your credential is now the
string `__aq_redacted__`".

### 3.3 Surfaces

* `get_config` gains `redacted: list[str]` (dotted paths) and
  `secret_placeholder: str` so clients can label the fields instead of
  guessing at the sentinel. Both are added to `GetConfigResponse`, so they are
  in `openapi.json` and both generated clients.
* System → Config lists the redacted paths for the selected section next to
  the existing env-reference panel, and says that placeholders left untouched
  keep the stored value.
* `aq system config get` warns on stderr when a section it printed contains
  redacted values.

Nothing gains a "reveal" flag: a reveal path would re-open the same
disclosure over the same API, which is the whole point of the finding. The
operator reads `~/.agent-queue/config.yaml` on the box to see a literal.

## 4. Tests

`tests/test_config_secrets.py` (pure layer) and additions to
`tests/test_config_editor.py` (handler round trip). Every fixture is a
`tmp_path` config; the sentinels are obvious fakes and the operator config is
never read or written.

Covered: DSN password redaction keeps host/user/db; env references survive;
numeric "token" knobs and `*_path` keys survive; provider token shapes are
caught under an innocuous key; saving an unrelated field keeps the stored
credential byte-identical; a deliberate new credential is written; an
unresolvable placeholder refuses the write and leaves the file unchanged;
`get_config` never returns a literal secret and reports the paths it hid.
