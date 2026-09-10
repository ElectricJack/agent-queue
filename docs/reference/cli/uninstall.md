# `aq uninstall`

Removes what [`aq install`](install.md) owns on this machine — and only that.

The command reads the installer's resume record, classifies every resource it
names, and prints the classification before it acts. The interesting part of an
uninstall is not what it removes but what it refuses to: a PostgreSQL server
that was already running when the installer found it, a Homebrew formula the
rest of your machine also uses, a provider CLI, a credential. None of those is
AQ's to delete.

Like `aq install`, this command runs without a daemon and talks to no API.

## Usage

```bash
aq uninstall                          # stop the AQ runtime, forget the records
aq uninstall --dry-run                # print the whole plan, change nothing
aq uninstall --remove-config          # also remove config.yaml and .env
aq uninstall --remove-data            # also remove ~/.agent-queue entirely
aq uninstall --remove-database        # also drop the AQ database and role
aq uninstall --all                    # every destructive scope, each confirmed
aq uninstall --non-interactive --all --yes --json   # unattended
```

## What the default does

With no flags, `aq uninstall` acts on the **runtime** scope only:

| Resource | What happens |
| --- | --- |
| `daemon` | `aq stop`. A daemon that is not running is the state we wanted, not an error. |
| `shell-profile` | The one marked block `aq install` appended to your login file is removed. The rest of the file, including a `brew shellenv` line you wrote yourself, is untouched — that is what the markers are for. |
| `install-record` | `~/.agent-queue/install-state.json` is deleted, **last**, so a run that failed halfway leaves the evidence behind for the next attempt. |

Your configuration, vault, projects, memory, task history and PostgreSQL
database are all left exactly where they are.

## The destructive scopes

Each needs its own flag *and* its own confirmation. `--all` selects exactly
these three; it never widens beyond them.

| Flag | Removes | Notes |
| --- | --- | --- |
| `--remove-config` | The `config.yaml` and `.env` the installer created | A `.bak` copy is written beside each file first. |
| `--remove-data` | The AQ home directory and everything under it | Vault, projects, memory, task history. Not undoable. |
| `--remove-database` | `DROP DATABASE` and `DROP ROLE` for the AQ database and role the installer created | Needs a PostgreSQL administrator connection; without one the item is reported with what to run by hand. |

Confirmation rules:

* **Interactive** (a terminal, no `--json`): one prompt per destructive scope,
  defaulting to *no*. Declining one **narrows the plan** and the rest of the run
  continues — declining the data question still removes the runtime.
* **Unattended** (`--non-interactive`, or `--json`): a destructive scope
  requires `--yes`. Without it the run stops at `needs_user` (exit 10) naming
  the flags, and nothing is removed.
* A scope with nothing *owned* in it is never confirmed. Selecting
  `--remove-database` on a host whose database was reused asks nothing, because
  it would do nothing.

## What is never removed

* **Anything the installer reused.** `owned: false` in the resume record is the
  boundary: a PostgreSQL server, a `git` on `PATH`, an existing directory. The
  installer did not create it, so uninstall does not delete it.
* **Credentials.** AQ never creates, reads, moves or deletes a provider
  credential store, so one is never an owned resource and uninstall never offers
  to remove it. The AQ database password lives in the `.env` file and goes with
  `--remove-config`.
* **A resource kind this build does not know how to remove.** An unrecognised
  kind is kept and reported, never guessed at.

## Reported, not removed

Some things the installer *did* install are still shared with the rest of your
machine. `aq uninstall` names them and the command that removes each, and stops
there — `tmux` is AQ's prerequisite and somebody else's terminal multiplexer.

| Kind | What you get |
| --- | --- |
| `provider-cli` | "Remove it with the provider's own uninstaller if you no longer want it." |
| `brew-formula` | `brew uninstall <formula>`, once you are sure nothing else needs it. |
| `postgres-server` | The package your platform's package manager installed, once you have confirmed no other database on it is in use. |

## Exit codes

The installer's table, unchanged:

| Outcome | Exit | Means |
| --- | --- | --- |
| `ready` | 0 | Everything selected was removed (or there was nothing to remove). |
| `needs_user` | 10 | A destructive scope was selected on an unattended run without `--yes`. Nothing was removed. |
| `invalid_input` | 11 | The resume record could not be read. |
| `failed` | 20 | At least one removal failed. The others still ran. |

A failed removal does not abandon the rest of the plan: aborting at the first
error leaves a host half-uninstalled with no record of which half. Every failure
is reported with what to do about it, and the resume record survives the run so
a second `aq uninstall` picks up where this one stopped.

## Machine-readable output

`--json` prints one object:

```json
{
  "schema_version": 1,
  "outcome": "ready",
  "exit_code": 0,
  "dry_run": false,
  "plan": {
    "scopes": ["runtime"],
    "state_path": "/home/you/.agent-queue/install-state.json",
    "destructive_scopes": [],
    "items": [
      {"kind": "daemon", "id": "http://127.0.0.1:8081/api", "action": "remove",
       "scope": "runtime", "reason": "owned by AQ and in scope runtime",
       "hint": "", "owned": true}
    ]
  },
  "removed": [
    {"kind": "daemon", "id": "http://127.0.0.1:8081/api", "action": "remove",
     "scope": "runtime", "reason": "owned by AQ and in scope runtime", "hint": "",
     "owned": true, "removed": true, "summary": "stopped the daemon at …",
     "error": null}
  ],
  "kept": [],
  "manual": [],
  "messages": []
}
```

* **`plan.items`** is every recorded resource with its `action` — `remove`,
  `keep` or `manual` — and the reason.
* **`removed`** is what each planned removal actually did; `error` is non-null
  on a failure and says what to do.
* **`kept`** and **`manual`** repeat the two refusal classes so a script does not
  have to filter `plan.items` itself.
* On `--dry-run`, `removed` is empty and no handler is called at all.

## No resume record

If there is no record, this machine has nothing `aq install` is known to own.
That is a finished uninstall, not a failure: the command says so and exits 0.

## Related pages

* [`aq install`](install.md) — what created the resources this removes.
* [CLI reference](README.md) — the whole `aq` surface.
* [Installer contract](../../plans/install-onboarding/contract.md) — the
  ownership and uninstall rules this command implements.
