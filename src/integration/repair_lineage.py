"""Validate cumulative repair dossiers and identify one stage's own commits."""

from sqlalchemy import select

from src.database.tables import integration_repair_stages
from src.git.manager import is_valid_git_oid


def introduced_repair_commits(stage, previous=None):
    def recorded(row):
        commits = (row["dossier"] or {}).get("repair_commits", []) if row else []
        if (not isinstance(commits, list)
            or any(not isinstance(sha, str) or not is_valid_git_oid(sha) for sha in commits)
            or len(set(commits)) != len(commits)):
            raise ValueError("stage has malformed or duplicate recorded repair commits")
        return commits

    current, inherited = recorded(stage), recorded(previous)
    if any(sha not in current for sha in inherited):
        raise ValueError("stage lost inherited repair commits")
    return [sha for sha in current if sha not in inherited]


async def introduced_repair_commits_on(conn, stage):
    previous = (await conn.execute(select(integration_repair_stages).where(
        integration_repair_stages.c.operation_id == stage["operation_id"],
        integration_repair_stages.c.ordinal < stage["ordinal"],
    ).order_by(integration_repair_stages.c.ordinal.desc()).limit(1))).mappings().one_or_none()
    return introduced_repair_commits(stage, previous)
