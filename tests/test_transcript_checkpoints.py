"""The durable transcript read position.

The mark exists so that a *second* reader of the same file — a relaunched
session, or a different session that resolved the same path — resumes where
the first one stopped instead of replaying the file.  That only works if the
mark never goes backwards on its own, which is what these pin.
"""

from __future__ import annotations

import pytest

from src.database import Database
from src.models import Project

PATH = "/home/x/.claude/projects/work/abc.jsonl"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "cp.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    yield database
    await database.close()


async def test_unseen_path_has_no_mark(db):
    assert await db.get_transcript_checkpoint(PATH) is None


async def test_set_then_get_round_trips(db):
    await db.set_transcript_checkpoint(
        PATH, byte_offset=1024, last_entry_uuid="u1", session_id="s1"
    )
    mark = await db.get_transcript_checkpoint(PATH)
    assert mark["byte_offset"] == 1024
    assert mark["last_entry_uuid"] == "u1"
    assert mark["session_id"] == "s1"
    assert mark["updated_at"] > 0


async def test_the_mark_never_moves_backwards(db):
    """Two readers on one file must not undo each other's progress.

    A session that is behind — mid-tick when another advanced the mark — would
    otherwise rewind it, and everything between the two offsets would be read,
    emitted and charged a second time.
    """
    await db.set_transcript_checkpoint(PATH, byte_offset=4096, session_id="ahead")
    await db.set_transcript_checkpoint(PATH, byte_offset=1024, session_id="behind")
    mark = await db.get_transcript_checkpoint(PATH)
    assert mark["byte_offset"] == 4096
    assert mark["session_id"] == "ahead"


async def test_zero_resets_a_rewritten_file(db):
    """The one legitimate rewind: the file itself was replaced.

    The caller only passes 0 after finding the file shorter than the mark, so
    a zero is a statement about the file, not a stale reader.
    """
    await db.set_transcript_checkpoint(PATH, byte_offset=4096, last_entry_uuid="u9")
    await db.set_transcript_checkpoint(PATH, byte_offset=0, session_id="reborn")
    mark = await db.get_transcript_checkpoint(PATH)
    assert mark["byte_offset"] == 0
    assert mark["last_entry_uuid"] is None


async def test_marks_are_per_path(db):
    await db.set_transcript_checkpoint(PATH, byte_offset=10)
    await db.set_transcript_checkpoint(PATH + ".2", byte_offset=20)
    assert (await db.get_transcript_checkpoint(PATH))["byte_offset"] == 10
    assert (await db.get_transcript_checkpoint(PATH + ".2"))["byte_offset"] == 20
    await db.delete_transcript_checkpoint(PATH)
    assert await db.get_transcript_checkpoint(PATH) is None
    assert await db.get_transcript_checkpoint(PATH + ".2") is not None
