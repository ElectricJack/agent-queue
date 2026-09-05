"""Merge project onboarding with the Playbook V2 cutover.

Revision ID: c7d8e9f0a1b2
Revises: b9f0c2d5e7a1, f9a1b2c3d4e5
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = ("b9f0c2d5e7a1", "f9a1b2c3d4e5")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
