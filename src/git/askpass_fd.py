#!/usr/bin/env python3
"""One-shot Git askpass helper backed by a daemon credential broker."""

from __future__ import annotations

import array
import os
import socket
import sys

REQUEST_PREFIX = b"aq.git-app-askpass.v1\0"
MAX_REQUEST_BYTES = 4096


def request_payload(authority: str, repository: str, prompt: str) -> bytes:
    payload = REQUEST_PREFIX + b"\0".join(
        value.encode("utf-8") for value in (authority, repository, prompt)
    )
    if len(payload) > MAX_REQUEST_BYTES or b"\n" in payload:
        raise ValueError("askpass request is invalid")
    return payload


def _request_credential(
    request_fd: int,
    *,
    authority: str,
    repository: str,
    prompt: str,
) -> str:
    request = socket.socket(fileno=os.dup(request_fd))
    reply, broker_reply = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        descriptors = array.array("i", [broker_reply.fileno()])
        request.sendmsg(
            [request_payload(authority, repository, prompt)],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptors)],
        )
        broker_reply.close()
        chunks: list[bytes] = []
        while chunk := reply.recv(64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return ""
    finally:
        request.close()
        reply.close()
        broker_reply.close()


def answer_prompt(
    prompt: str,
    request_fd: int,
    username: str,
    authority: str,
    repository: str,
) -> str:
    username_authority = authority.replace(f"{username}@", "", 1)
    if prompt == f"Username for '{username_authority}': ":
        return username
    if prompt == f"Password for '{authority}': ":
        return _request_credential(
            request_fd,
            authority=authority,
            repository=repository,
            prompt=prompt,
        )
    return ""


def main() -> int:
    try:
        request_fd = int(os.environ["AQ_GIT_APP_REQUEST_FD"])
        username = os.environ["AQ_GIT_APP_USERNAME"]
        authority = os.environ["AQ_GIT_APP_AUTHORITY"]
        repository = os.environ["AQ_GIT_APP_REPOSITORY"]
        prompt = sys.argv[1] if len(sys.argv) > 1 else ""
        sys.stdout.write(
            answer_prompt(prompt, request_fd, username, authority, repository)
        )
        return 0
    except (KeyError, ValueError, OSError, UnicodeDecodeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
