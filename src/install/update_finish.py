"""The half of `aq update` that runs on the code the update just pulled.

`aq update` imports its code, then moves the checkout forward -- so from that
moment the process doing the update is the *previous* version of AQ, and what
it knows about the new one is a guess: which modules exist, how dependencies
are installed and the dashboard is built, what a healthy new daemon answers.
A guess that goes stale breaks the first update from every older installation.

So the old process stops at the pull and starts this module from the new
checkout (``python -m src.install.update_finish``, working directory = the
checkout).  Everything here is the new version's own knowledge of itself:

* reinstall Python dependencies when the update changed them,
* rebuild the dashboard when its build inputs changed,
* start the daemon and decide whether it is healthy.

It reports on stdout, one JSON object per line (:data:`~.update.EVENT_KEY`),
and exits non-zero when a step failed.  Rolling back is the caller's job: that
process *is* the version being rolled back to.

Two rules keep the hand-off working across versions, because the caller is
always an older AQ than this file:

* **The command line only grows.**  Never drop or rename an argument, give
  every new one a default, and ignore what is not recognised.
* **Import nothing the new code has not installed yet.**  This runs before
  `pip install`, so module level is the standard library and `src.install`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from .update import (
    EVENT_DAEMON_STARTING,
    EVENT_KEY,
    EVENT_STEP,
    Host,
    UpdatePlan,
    _daemon_address,
    _describe,
    _git,
    _install_dependencies,
    _rebuild_dashboard,
    _start_daemon,
    _StepFailed,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.install.update_finish",
        description="Finish an `aq update` on the code it pulled. Run by `aq update`, not by hand.",
        # A future argument must never be mistaken for the prefix of one of these.
        allow_abbrev=False,
    )
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--previous", default="", help="the commit the update started from")
    parser.add_argument("--target", default="", help="the commit the checkout is on now")
    parser.add_argument("--aq", default="aq")
    parser.add_argument("--system", default=sys.platform)
    parser.add_argument("--arch", default="")
    parser.add_argument("--extras", default="cli")
    parser.add_argument("--start-daemon", action="store_true")
    return parser


def _changed(host: Host, checkout: Path, previous: str, target: str) -> tuple[str, ...] | None:
    """What the update changed, or ``None`` when Git cannot say."""
    if not previous or not target:
        return None
    diff = _git(host.execute, checkout, "diff", "--name-only", previous, target)
    if not diff.ok:
        return None
    return tuple(line for line in diff.out.splitlines() if line.strip())


def finish(
    checkout: Path,
    host: Host,
    step: Callable[..., None],
    *,
    previous: str,
    target: str,
    start_daemon: bool,
    starting: Callable[[], None] = lambda: None,
) -> None:
    changed = _changed(host, checkout, previous, target)
    plan = UpdatePlan(
        checkout=checkout,
        branch="",
        upstream="",
        current=previous,
        target=target,
        shallow=False,
        changed=changed or (),
    )
    # Not knowing what changed is not knowing the dependencies did not.
    if changed is None or plan.dependencies:
        _install_dependencies(host, checkout)
        step("Reinstall Python dependencies", True)

    _rebuild_dashboard(host, checkout, step)

    if start_daemon:
        base, healthy = _daemon_address(host)
        starting()
        _start_daemon(host, base, checkout, healthy)
        step("Start the daemon", True, "agent sessions are re-adopted")


def main(
    argv: Sequence[str] | None = None,
    *,
    host_factory: Callable[..., Host] = Host,
    out: TextIO | None = None,
) -> int:
    args, _from_a_newer_updater = _parser().parse_known_args(argv)
    stream = sys.stdout if out is None else out

    def emit(kind: str, **fields: object) -> None:
        # Flushed line by line: if this process is killed, the caller still
        # learns whether the daemon had been started.
        print(json.dumps({EVENT_KEY: kind, **fields}), file=stream, flush=True)

    def step(name: str, ok: bool, message: str = "") -> None:
        emit(EVENT_STEP, name=name, ok=ok, message=message)

    fields: dict[str, object] = {
        "python": sys.executable,
        "aq": args.aq,
        "system": args.system,
        "arch": args.arch,
        "extras": [extra for extra in args.extras.split(",") if extra],
    }
    if args.state_dir is not None:
        fields["state_dir"] = args.state_dir
    try:
        finish(
            args.checkout,
            host_factory(**fields),
            step,
            previous=args.previous,
            target=args.target,
            start_daemon=args.start_daemon,
            starting=lambda: emit(EVENT_DAEMON_STARTING),
        )
    except _StepFailed as failure:
        step(failure.name, False, failure.message)
        return 1
    except Exception as error:  # noqa: BLE001 - reported, so the caller rolls back
        step("Finish the update on the new code", False, f"unexpected {_describe(error)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
