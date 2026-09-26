"""Read-only, bounded views of the runner's retained logical byte stream."""

from __future__ import annotations

import base64
from pathlib import Path

from src.jobs.artifacts import OutputStore, job_directory


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
