#!/usr/bin/env python3
"""One-shot Git askpass helper; the credential is inherited on an FD."""

from __future__ import annotations

import os
import sys


def answer_prompt(prompt: str, token_fd: int, username: str) -> str:
    if prompt.lower().startswith("username"):
        return username
    if prompt.lower().startswith("password"):
        return os.read(token_fd, 1024 * 1024).decode("utf-8")
    return ""


def main() -> int:
    try:
        token_fd = int(os.environ["AQ_GIT_APP_TOKEN_FD"])
        username = os.environ["AQ_GIT_APP_USERNAME"]
        prompt = sys.argv[1] if len(sys.argv) > 1 else ""
        sys.stdout.write(answer_prompt(prompt, token_fd, username))
        return 0
    except (KeyError, ValueError, OSError, UnicodeDecodeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
