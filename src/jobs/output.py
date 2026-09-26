"""Read-only, bounded views of the runner's retained logical byte stream."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from pathlib import Path

from src.jobs.artifacts import OutputStore, job_directory
from src.jobs.policy import TERMINAL


def read_output(data_dir: Path, job: dict, after: int, limit: int) -> dict:
    store = OutputStore(
        job_directory(data_dir, job["id"]),
        head_bytes=job["contract"]["head_bytes"],
        tail_bytes=job["contract"]["tail_bytes"],
        readonly=True,
    )
    try:
        response = store.read(after, limit)
        for chunk in response["chunks"]:
            # Text length is not a byte cursor, especially for invalid UTF-8.
            chunk["next"] = chunk["offset"] + len(chunk["data"])
            chunk["data_base64"] = base64.b64encode(chunk["data"]).decode("ascii")
            chunk["data"] = chunk["data"].decode("utf-8", "replace")
        return response
    finally:
        store.close()


def output_frames(response: dict, after: int) -> list[dict]:
    frames = [
        {"type": "chunk", **chunk} for chunk in response["chunks"] if chunk["next"] > after
    ] + [
        {"type": "gap", "after": max(after, gap["after"]), "next": gap["next"]}
        for gap in response["gaps"]
        if gap["next"] > after
    ]
    frames.sort(key=lambda frame: frame.get("offset", frame.get("after", 0)))
    return frames


@dataclass(eq=False)
class Attachment:
    principal: str
    cursor: int
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=8))
    disconnected: bool = False


class OutputHub:
    """One poller per watched job, with no dependency on execution lifetime.

    Subscribers carry only bounded queues, never output files or child pipes.
    A blocked HTTP sender loses its subscription and resumes from its last
    delivered byte cursor. The runner continues draining independently.
    """

    def __init__(self, handler, *, poll_seconds=0.25, queue_size=8):
        self.handler = handler
        self.poll_seconds, self.queue_size = poll_seconds, queue_size
        self.attachments: dict[str, set[Attachment]] = {}
        self.readers: dict[str, asyncio.Task] = {}

    def subscribe(self, job_id: str, principal: str, after: int) -> Attachment:
        subscribers = self.attachments.setdefault(job_id, set())
        if sum(a.principal == principal for a in subscribers) >= 2:
            raise ValueError("jobs.attachment_limit")
        attachment = Attachment(principal, after, asyncio.Queue(maxsize=self.queue_size))
        subscribers.add(attachment)
        if job_id not in self.readers:
            self.readers[job_id] = asyncio.create_task(self._poll(job_id))
        return attachment

    def unsubscribe(self, job_id: str, attachment: Attachment) -> None:
        subscribers = self.attachments.get(job_id, set())
        subscribers.discard(attachment)
        if not subscribers:
            task = self.readers.pop(job_id, None)
            if task:
                task.cancel()
            self.attachments.pop(job_id, None)

    def _send(self, attachment: Attachment, frame: dict) -> None:
        if attachment.disconnected:
            return
        try:
            attachment.queue.put_nowait(frame)
        except asyncio.QueueFull:
            # Clear queued data: the generator reports its delivered cursor,
            # not the poller's cursor, when it sends the disconnect frame.
            while not attachment.queue.empty():
                attachment.queue.get_nowait()
            attachment.disconnected = True
            attachment.queue.put_nowait({"type": "disconnect", "reason": "slow_reader"})

    async def _poll(self, job_id: str) -> None:
        from src.commands.principal import ExecutionPrincipal, principal_context

        try:
            while True:
                subscribers = [a for a in self.attachments.get(job_id, ()) if not a.disconnected]
                if not subscribers:
                    return
                with principal_context(ExecutionPrincipal.service("job_output")):
                    job_response = await self.handler.execute("job_get", {"job_id": job_id})
                if not job_response.get("success"):
                    for attachment in subscribers:
                        self._send(attachment, {"type": "error", "error": "not_found"})
                    return
                job = job_response["job"]
                after = min(a.cursor for a in subscribers)
                with principal_context(ExecutionPrincipal.service("job_output")):
                    response = await self.handler.execute(
                        "job_logs", {"job_id": job_id, "after": after, "limit": 65536}
                    )
                if response.get("error") == "logs_expired":
                    # Cancellation before spawn has no output files. A viewer
                    # already attached still needs its durable terminal result.
                    with principal_context(ExecutionPrincipal.service("job_output")):
                        latest = await self.handler.execute("job_get", {"job_id": job_id})
                    if latest.get("success"):
                        job = latest["job"]
                # Attachments can arrive while the shared read is awaiting I/O.
                # Include them before deciding that a terminal page is done.
                subscribers = [a for a in self.attachments.get(job_id, ()) if not a.disconnected]
                if response.get("success"):
                    for attachment in subscribers:
                        if attachment.cursor < after:
                            continue  # next read starts at this new subscriber's cursor
                        # A newer subscriber may start inside this shared page.
                        for frame in output_frames(response, attachment.cursor):
                            if frame["type"] == "chunk" and frame["offset"] < attachment.cursor:
                                # Re-read on the next pass at this exact cursor;
                                # decoding then slicing by characters is unsafe.
                                break
                            self._send(attachment, frame)
                            attachment.cursor = frame["next"]
                    if job["state"] in TERMINAL and all(
                        a.cursor >= response["seen"] for a in subscribers if not a.disconnected
                    ):
                        for attachment in subscribers:
                            self._send(
                                attachment,
                                {
                                    "type": "terminal",
                                    "next": attachment.cursor,
                                    "state": job["state"],
                                    "result": job["result"],
                                },
                            )
                        return
                elif response.get("error") == "logs_expired" and job["state"] in TERMINAL:
                    for attachment in subscribers:
                        seen = max(
                            attachment.cursor,
                            (job.get("result") or {}).get(
                                "output_bytes_seen",
                                0,
                            ),
                        )
                        if seen > attachment.cursor:
                            self._send(
                                attachment,
                                {"type": "gap", "after": attachment.cursor, "next": seen},
                            )
                        self._send(
                            attachment,
                            {
                                "type": "terminal",
                                "next": seen,
                                "state": job["state"],
                                "result": job["result"],
                            },
                        )
                    return
                elif response.get("error") != "logs_not_ready":
                    for attachment in subscribers:
                        self._send(attachment, {"type": "error", **response})
                    return
                elif job["state"] in TERMINAL:
                    for attachment in subscribers:
                        self._send(
                            attachment,
                            {
                                "type": "terminal",
                                "next": attachment.cursor,
                                "state": job["state"],
                                "result": job["result"],
                            },
                        )
                    return
                if not response.get("success") or response["next"] >= response["seen"]:
                    await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            for attachment in self.attachments.get(job_id, ()):
                self._send(attachment, {"type": "error", "error": "output_unavailable"})
        finally:
            if self.readers.get(job_id) is asyncio.current_task():
                self.readers.pop(job_id, None)
