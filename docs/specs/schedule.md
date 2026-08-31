---
tags: [spec, schedule, core]
---

# Schedule Matching Spec

## 1. Overview

`src/schedule.py` evaluates embedded schedule definitions against a point in
time. It is a pure function library — no I/O, no LLM, no state beyond the
`last_run` timestamp the caller passes in. The in-tree consumer is the plugin
cron scheduler (`PluginRegistry.tick_cron`, cron expressions only); the
structured `times`/`days_of_week`/`days_of_month` format is part of the same
public contract and is specified here.

The synthetic `timer.*` / `cron.HH:MM` playbook triggers are a **different**
subsystem (`src/timer_service.py`, see [[design/playbooks]] §7) and do not use
this module.

## Source Files
- `src/schedule.py`

## 2. Schedule format

```json
{
    "times": ["02:00", "14:30"],
    "days_of_week": ["mon", "wed", "fri"],
    "days_of_month": [1, 15],
    "cron": "0 2 * * 1-5"
}
```

- When `cron` is present it is used **exclusively**; the structured fields are
  ignored.
- Structured fields combine with AND; values within one field are OR.
- An empty schedule dict always matches (no constraints).

## 3. Matching (`matches_schedule`)

- All evaluation is in UTC; naive `now`/`last_run` datetimes are assumed UTC.
- `times` entries are `HH:MM` (24h). A time matches when the current
  seconds-into-the-day is within `tolerance_seconds` (default 60) of the
  target, **with midnight wraparound**: the distance between 23:59:30 and
  00:00 is 30 seconds, not 86 370. A window is therefore a contiguous span of
  width 2×tolerance centered on the target, possibly spanning midnight.
- Invalid `times` entries and unknown day names are logged and skipped
  (non-matching), never raised.
- Cron: standard 5 fields (minute hour dom month dow, dow 0=Monday), with
  `*`, `*/N`, `N-M`, `N-M/S`, lists, and exact values. A step below 1
  (e.g. `*/0`) is invalid and non-matching — logged, never a
  `ZeroDivisionError` (EVT-1). Malformed fields are non-matching.

## 4. Dedup (`last_run`)

`matches_schedule` must not report a match twice for the same schedule window.

- **Cron:** a match is suppressed when `last_run` falls in the same calendar
  minute as `now`.
- **`times` windows:** a match is suppressed when `now` and `last_run` fall in
  the **same window occurrence**. Decided semantics (EVT-2): a tolerance
  window that spans midnight is ONE window — the same wraparound rule used
  for matching applies to dedup, so a `times: ["00:00"]` hook that fired at
  23:59:30 does not fire again at 00:00:30. Occurrences of the same target
  are 24h apart, so two timestamps share an occurrence **iff** both are
  within tolerance of the target (wraparound distance) **and** they are at
  most 2×tolerance apart in real elapsed time. The elapsed-time guard
  replaces the previous same-calendar-date check, which both split the
  cross-midnight window (double fire, the EVT-2 bug) and was the only thing
  preventing a fire 24h earlier from suppressing today's window.

Tolerances of 12 hours or more are outside the contract: windows would
overlap their own wraparound reflection and dedup is unspecified there.

## 5. Next-run computation

`next_run_time` scans forward minute-by-minute (up to `max_lookahead_hours`,
default 168) calling `matches_schedule`, so it inherits every rule above,
including window dedup against `last_run`.
