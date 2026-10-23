---
id: quilt-health-watch
name: Quilt Trader health watch
version: 1
scope: project:quilt-trader
enabled: false
triggers:
  - cron.03:00
  - timer.15m
  - timer.1h
---

# Quilt Trader health watch

Keeps Quilt Trader online and checked for errors. The coordinator was down from
2026-09-16 to 2026-09-26 and nobody noticed; this policy exists so that cannot
happen again. Three live deployments on worker Trader1 trade real money (Alpaca
Personal, Tradier Rollover IRA, Tradier Personal).

This is the backstop and error monitor, not the primary restarter: cron already
runs `~/.quilt/bin/coord-watchdog.sh` every 2 minutes and at boot. Every rule
below schedules one check task, reads the verdict that task wrote, and alerts
Jack when it has to. The playbook itself only calls `create_task` (through its
agent task), `get_task` and `message_send`. It never touches orders,
deployments, algorithm config or settings.

## Schedule

Local time is US Pacific; the NYSE opens at 06:30 local.

* `cron.03:00` starts the pre-market check, early enough to recover before the
  open.
* `timer.15m` checks every 15 minutes during the market window.
* `timer.1h` checks hourly outside it.

Playbook V2 expressions cannot read a weekday or a local hour. They can test
whether the event's `tick_time` (UTC, ISO 8601) contains a substring, so the
market window is written as a UTC hour window: `tick_time` contains `T13:`,
`T14:`, `T15:`, `T16:`, `T17:`, `T18:`, `T19:`, `T20:`, `T21:0`, `T21:1` or
`T21:2`. That is 13:00-21:29 UTC: 06:00-14:29 in Pacific daylight time and
05:00-13:29 in Pacific standard time, so it covers 06:00-13:30 local all year
without a daylight-saving table. Weekends get the same 15-minute cadence,
because no V2 expression can tell a Saturday from a Monday.

`cron.HH:MM` rules are not used for the 15-minute slots on purpose: after a
daemon restart every slot that already passed today fires at once
(`src/timer_service.py`), which would start dozens of checks together.

## The check task

Each rule's first step is an agent task on intelligence class `fast-low` (class
`fast-low`) in project `quilt-trader`, filed under the standing container
`quilt-health-checks` ("Quilt Trader health checks"), priority 20. The task
narrows its harness tools to `Bash`, `Read`, `Grep` and `Glob`. The agent runs
`scripts/health_watch.py`, which does every mechanical part deterministically:

* probes `http://localhost:8000/api/health`; if it is down, runs the watchdog
  script once and waits up to 90 seconds for health;
* reads `~/.quilt/log/watchdog.log` for restarts since the last check, and
  flags a crash loop at three restarts within an hour;
* reads Trader1 and every deployment with `quilt --json worker list` and
  `quilt --json deploy list`, keeping only names and statuses (the worker JSON
  carries an install token);
* scans only the new bytes of `~/.quilt/log/coord.log` (at most 64 MB per
  check) for ERROR/CRITICAL records and bare tracebacks, groups them by logger,
  message and exception with numbers and ids normalised, and ignores the
  quantstats/numpy RuntimeWarning noise;
* keeps its offsets, filed signatures and alert times in
  `~/.quilt/run/health-watch/state.json`, so a signature is filed once and an
  alert condition is raised once, then again hourly while it persists;
* offers at most five new signatures per check, each with a ready
  `aq task create --root` command whose title and excerpt it wrote to files.

It prints one verdict line: `QUILT-HEALTH: OK`, `QUILT-HEALTH: ALERT`,
`QUILT-HEALTH: KNOWN` (a condition already alerted within the hour) or
`QUILT-HEALTH: CLEARED` (every alerted condition is gone), followed by a short
status. Alert conditions are: the coordinator could not be made healthy, a
crash loop, a worker that is not online, a deployment that is not `running`,
and workers/deployments that could not be read.

The agent files one bugfix task per new error signature and closes with the
verdict line as the first line of its summary. That summary is the run's status
note. The exact task description the agent receives is:

```text
You are the Quilt Trader health check. Read-only: never place or cancel orders;
never create, change, stop, start or restart deployments; never change algorithm
config, accounts or coordinator settings; never run `quilt up` or `quilt down`
yourself; never edit, commit or push anything. The only repair allowed is the
single watchdog run that `health_watch.py check` performs by itself. Do not
message anyone: the playbook reads your close summary and sends any alert.

1. From your quilt-trader workspace run:
     python3 scripts/health_watch.py check | tee .aq/health-watch.json
   It can take about four minutes when the coordinator is down. If the command
   fails or prints no JSON, skip to step 4 and close with the summary
   "QUILT-HEALTH: ALERT - health_watch.py failed: <last error line>".
2. For each entry of errors.to_file (the script offers at most 5; later checks
   offer the rest), run its file_command exactly as printed. It files one
   bugfix task at the project root, reading the title and the log excerpt from
   files the script wrote. Note the task id it prints.
3. Record what you did:
     python3 scripts/health_watch.py commit --scan <scan_id> \
       --filed <id>=<task id> [--filed ... for each task you filed]
4. Close. The summary's first line must be verdict_line exactly as printed;
   add one line per bugfix task you filed ("filed <task id>: <signature>"):
     aq task close --outcome pass --work-outcome no-op --summary "<verdict_line>
     <filed lines>"
```

## Rule: pre-market

On `cron.03:00`. No guard.

1. Start the check task described above with mode "pre-market (03:00, before
   the 06:30 open)" and the event's `tick_time` in its title and description.
   Wait up to 40 minutes for it and bind it as `premarket_check`. `completed`
   continues to step 2. `failed`, `timed_out`, `cancelled`, or a task that could
   not be created, continues to step 7.
2. Call `get_task` with `task_id` from `premarket_check` and bind the result as
   `premarket_report`. `read` continues to step 3; `rejected` or a runtime
   error continues to step 7.
3. Decide on the summary of `premarket_report` (its completion record):
   containing `QUILT-HEALTH: ALERT` goes to step 4; `QUILT-HEALTH: CLEARED`
   goes to step 5; `QUILT-HEALTH: OK` or `QUILT-HEALTH: KNOWN` ends the rule;
   anything else goes to step 6.
4. Call `message_send` with `project_id` `quilt-trader`, `to_kind` `user`,
   `to_id` `dashboard`, `from_kind` `system`, `from_id`
   `playbook:quilt-health-watch`, `priority` 10, `subject` "Quilt Trader
   PRE-MARKET ALERT: fix before the 06:30 PT open" and a `body` that says the
   03:00 check found a problem that needs attention before NYSE opens at 06:30
   PT, quotes the check's summary, and names the check task and playbook run.
   `queued` ends the rule.
5. Call `message_send` as in step 4 with `priority` 50, `subject` "Quilt
   Trader recovered" and a `body` quoting the summary. `queued` ends the rule.
6. Call `message_send` as in step 4 with `subject` "Quilt Trader pre-market
   check gave no verdict" and a `body` saying the check finished without a
   `QUILT-HEALTH:` line, so Quilt Trader must be checked by hand before 06:30
   PT. `queued` ends the rule as failed.
7. Call `message_send` as in step 4 with `subject` "Quilt Trader pre-market
   check did not complete" and a `body` saying Quilt Trader is unmonitored and
   must be checked by hand before 06:30 PT, naming the playbook run. `queued`
   ends the rule as failed.

## Rule: market-hours

On `timer.15m`, guarded to the market window above: `tick_time` contains one of
the eleven UTC substrings. Outside the window the tick starts nothing.

The steps are the pre-market steps with mode "market hours", a 20-minute wait
bound as `market_check`, the `get_task` result bound as `market_report`,
subjects "Quilt Trader ALERT (market hours)", "Quilt Trader recovered", "Quilt
Trader health check gave no verdict" and "Quilt Trader health check did not
complete", and bodies that say the market is open instead of naming 06:30.

## Rule: off-hours

On `timer.1h`, guarded to the complement: `tick_time` contains none of the
eleven substrings.

The steps are the pre-market steps with mode "off hours", a 40-minute wait bound
as `offhours_check`, the `get_task` result bound as `offhours_report`, subjects
"Quilt Trader ALERT", "Quilt Trader recovered", "Quilt Trader health check gave
no verdict" and "Quilt Trader health check did not complete", and bodies without
the market wording.

## Failure handling, uniformly

No step retries. Any failure to schedule, finish or read a check becomes a
message to Jack and a `failed` run, so an unmonitored stretch is never silent.
A `message_send` that is `rejected` or errors ends the run as failed and is
visible in playbook health. The next tick starts over from a fresh check;
`health_watch.py` state makes a repeated check neither re-file a signature nor
re-alert a condition inside the hour.
