"""Finite command translation and queue clients; these never execute commands."""

from __future__ import annotations

import asyncio
import shlex

from src.jobs.policy import JobError, TERMINAL


def finite_command(command: str | list[str]) -> tuple[str, list[str]]:
    """Translate existing command syntax to a server-owned finite preset."""
    try:
        argv = shlex.split(command) if isinstance(command, str) else list(command)
    except ValueError as exc:
        raise JobError("jobs.preset_denied") from exc
    for prefix, preset in (
        (["aq", "test"], "test"),
        (["pytest"], "test"),
        (["python", "-m", "pytest"], "test"),
        (["python3", "-m", "pytest"], "test"),
        (["ruff", "check"], "lint"),
        (["python", "-m", "ruff", "check"], "lint"),
        (["python3", "-m", "ruff", "check"], "lint"),
        (["npm", "run", "build"], "build"),
        (["scripts/e2e-smoke.sh"], "e2e"),
    ):
        if argv[:len(prefix)] == prefix:
            args = argv[len(prefix):]
            if any(a in {";", "&&", "||", "|", "&", ">", ">>", "<"} for a in args):
                break
            if preset in {"build", "e2e"} and args:
                break
            return preset, args
    raise JobError("jobs.preset_denied")


class PublisherJobs:
    """Trusted publisher adapter. Mutations stay on CommandHandler's path."""

    def __init__(self, handler):
        self._handler = handler

    @property
    def handler(self):
        return self._handler() if callable(self._handler) else self._handler

    async def submit(self, **args):
        response = await self.handler._cmd_job_submit_integration(args)
        if not response["success"]:
            raise JobError(response.get("error_code") or response["error"])
        return response["job"]

    async def wait(self, job, *, poll_seconds=1.0):
        while job["state"] not in TERMINAL:
            await self.handler._cmd_job_reconcile({})
            job = await self.handler.db.get_job(job["id"])
            if job is None:
                raise JobError("jobs.not_found")
            if job["state"] not in TERMINAL:
                await asyncio.sleep(poll_seconds)
        return job
