#!/usr/bin/env python3
"""One-shot Git askpass helper; the credential is inherited on an FD."""

from __future__ import annotations

import os
import sys


def answer_prompt(prompt: str, token_fd: int, username: str, authority: str) -> str:
    username_authority = authority.replace(f"{username}@", "", 1)
    if prompt == f"Username for '{username_authority}': ":
        return username
    if prompt == f"Password for '{authority}': ":
        chunks: list[bytes] = []
        while chunk := os.read(token_fd, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    return ""


def main() -> int:
    try:
        token_fd = int(os.environ["AQ_GIT_APP_TOKEN_FD"])
        username = os.environ["AQ_GIT_APP_USERNAME"]
        authority = os.environ["AQ_GIT_APP_AUTHORITY"]
        prompt = sys.argv[1] if len(sys.argv) > 1 else ""
        sys.stdout.write(answer_prompt(prompt, token_fd, username, authority))
        return 0
    except (KeyError, ValueError, OSError, UnicodeDecodeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
