---
id: provider-usage-probe
name: Provider usage probe
version: 1
scope: system
enabled: true
triggers:
  - timer.10m
---

# Provider usage probe

Every `timer.10m` tick begins the one `probe-claude-usage` rule. Claude
publishes no local rate-limit file the way Codex does, so the only way to
learn what the subscription has left is to ask the CLI, and the only thing
that can ask it on a schedule is a playbook.

Ten minutes is roughly 144 probes a day of a local command that is verified
free: a live `claude -p "/usage" --output-format json` reports `num_turns: 0`
and `total_cost_usd: 0`, so the probe bills none of the quota it reports. The
weekly windows it reads move in whole percent, and the storage layer drops a
reading identical to the newest one already stored, so a ten-minute cadence
against an idle account writes nothing at all.

The playbook holds no state and makes no decision. It runs one command and
ends; the command owns the subprocess, the parser, the snapshot write and the
record of its own health, and `aq doctor --check providers.claude_usage` is
what turns a probe that stopped working into a human's problem.

## Rule: probe-claude-usage

There is no guard beyond the trigger. The rule performs one step and then ends.

1. Call `provider_usage_probe` with `provider` `claude`. Bind the result as
   `usage`. The outcomes `probed` (a reading was stored), `unparsed` (the CLI
   answered with text no limit-line regex matched), `not_applicable` (an
   API-key account has no subscription window to report), `unavailable` (this
   box has no Claude CLI) and `disabled` (`providers.claude.usage_probe_enabled`
   is false) all end the rule successfully. A `rejected` or `runtime_error`
   outcome fails it.

## Failure handling, uniformly

The rule has no retry: the next tick is ten minutes away and is a better
retry than any the run could schedule. A failed step ends the run with a
`failed` terminal so the run overlay shows what broke — a non-zero
exit, a body that was not the JSON envelope we asked for — and the next tick
starts a fresh run. A timeout reports `unavailable` successfully, like a missing CLI.

Nothing about a failure touches stored state. The last good snapshot survives
every failure mode, and the API and the dashboard card label it stale from
`last_seen_at` rather than pretending it is current. A blank card beats a
wrong number, and a number labelled stale beats a blank card.

Four of the five success outcomes store no snapshot, and that is deliberate:
a box without the CLI, an account billed per token, and a probe an operator
turned off are all facts about the install rather than broken steps. A step
that failed every ten minutes on any of them would fill the run overlay with
noise nobody can act on.

Each probe leaves a zero-turn session file under `~/.claude/projects/`. The
transcript watcher only reads transcripts belonging to live session rows, so
those files are inert; they are not suppressed and do not need to be.
