"""Release migration checks use literal source, never execute branch code."""

import pytest

from src.database.additive_migrations import MigrationTree, check_range


def revision(rev="b", parent="a", body="pass", imports=""):
    return (
        "from alembic import op\nimport sqlalchemy as sa\n" + imports + "\n"
        f"revision = {rev!r}\ndown_revision = {parent!r}\n"
        "def upgrade():\n" + "\n".join("    " + line for line in body.splitlines()) + "\n"
        "def downgrade():\n    op.drop_table('allowed_in_downgrade')\n"
    )


BASE = {"migrations/versions/a.py": revision("a", None)}


@pytest.mark.parametrize(
    "body",
    [
        "op.add_column('tasks', sa.Column('note', sa.Text(), nullable=True))",
        "op.add_column('tasks', sa.Column('note', sa.Text()))",
        "op.add_column('tasks', sa.Column('flag', sa.Boolean(), nullable=False, "
        "server_default=sa.false()))",
        "op.create_table('extra', sa.Column('id', sa.Integer(), primary_key=True))",
        "op.create_index('idx', 'tasks', ['note'])",
        "op.create_check_constraint('ck_tasks_note', 'tasks', 'note IS NULL')",
        "pass",
    ],
)
def test_additions_pass_and_downgrade_is_ignored(body):
    assert check_range(BASE, {**BASE, "migrations/versions/b.py": revision(body=body)}) == []


@pytest.mark.parametrize(
    "body",
    [
        "op.drop_table('tasks')",
        "op.drop_column('tasks', 'title')",
        "op.drop_index('idx')",
        "op.rename_table('tasks', 'jobs')",
        "op.alter_column('tasks', 'title', new_column_name='summary')",
        "op.alter_column('tasks', 'title', type_=sa.String(1))",
        "op.alter_column('tasks', 'title', nullable=False)",
        "op.add_column('tasks', sa.Column('note', sa.Text(), nullable=False))",
        "op.add_column('tasks', sa.Column('note', sa.Text(), nullable=False, default='x'))",
        "op.add_column('tasks', sa.Column('note', sa.Text(), nullable=False, "
        "server_default=maybe_none))",
        "op.add_column('tasks', sa.Column('id', sa.Integer(), primary_key=True))",
        "op.add_column('tasks', column_from_elsewhere)",
        "op.execute('ALTER TABLE tasks DROP COLUMN title')",
        "op.get_bind().execute(sa.text('DROP TABLE tasks'))",
        "sa.engine.Connection.exec_driver_sql(op.get_bind(), 'DROP TABLE tasks')",
        "imported_helper()",
    ],
)
def test_breaking_or_unproven_upgrade_fails_with_location(body):
    findings = check_range(BASE, {**BASE, "migrations/versions/b.py": revision(body=body)})
    assert findings
    assert all(f.path == "migrations/versions/b.py" and f.line >= 6 for f in findings)


def test_reachable_helper_and_import_alias_cannot_hide_drop():
    source = revision(body="helper()", imports="from alembic.op import drop_table as remove")
    source += "\ndef helper():\n    remove('tasks')\n"
    assert "drop_table" in str(check_range(BASE, {**BASE, "migrations/versions/b.py": source})[0])


def test_top_level_operation_is_checked_without_executing_it(tmp_path):
    marker = tmp_path / "must_not_exist"
    source = revision() + f"\nopen({str(marker)!r}, 'w').write('executed')\n"
    assert check_range(BASE, {**BASE, "migrations/versions/b.py": source})
    assert not marker.exists()


def test_existing_revision_cannot_be_deleted_modified_or_renamed():
    modified = {**BASE, "migrations/versions/b.py": revision()}
    modified["migrations/versions/a.py"] += "# changed historical revision\n"
    assert "modified" in str(check_range(BASE, modified)[0])
    renamed = {"migrations/versions/renamed.py": BASE["migrations/versions/a.py"]}
    assert "modified" in str(check_range(BASE, renamed)[0])
    with pytest.raises(ValueError, match="does not descend"):
        check_range(BASE, {"migrations/versions/b.py": revision(parent=None)})


def test_multiple_heads_duplicate_ids_and_cycles_are_reported():
    with pytest.raises(ValueError, match="multiple.*heads.*b.*c"):
        MigrationTree({**BASE, "b.py": revision(), "c.py": revision("c")})
    with pytest.raises(ValueError, match="duplicate revision b"):
        MigrationTree({**BASE, "b.py": revision(), "copy.py": revision()})
    with pytest.raises(ValueError, match="cycle"):
        MigrationTree({"a.py": revision("a", "b"), "b.py": revision()})


def test_merge_revision_checks_both_branches():
    graph = {
        **BASE,
        "b.py": revision(),
        "c.py": revision("c", body="op.drop_table('tasks')"),
        "merge.py": revision("d", ("b", "c")),
    }
    assert "drop_table" in str(check_range(BASE, graph)[0])


def test_depends_on_does_not_hide_alembic_multiple_heads():
    source = revision("b", None) + "\ndepends_on = 'a'\n"
    with pytest.raises(ValueError, match="multiple.*heads.*a.*b"):
        MigrationTree({**BASE, "b.py": source})


def test_function_defaults_execute_even_on_unused_downgrade():
    source = revision() + "\ndef unused(value=op.drop_table('tasks')):\n    pass\n"
    assert "drop_table" in str(check_range(BASE, {**BASE, "b.py": source})[0])


def test_empty_or_missing_parent_graph_fails():
    with pytest.raises(ValueError, match="missing heads"):
        MigrationTree({})
    with pytest.raises(ValueError, match="missing parent"):
        MigrationTree({"b.py": revision()})


async def test_cli_reads_pinned_range_and_reports_heads(tmp_path):
    import subprocess

    from scripts.check_additive_migrations import inspect_range

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    git("config", "user.email", "fixture@example.test")
    git("config", "user.name", "Fixture")
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "a.py").write_text(BASE["migrations/versions/a.py"])
    git("add", ".")
    git("commit", "-m", "base")
    git("tag", "v1")
    (versions / "b.py").write_text(
        revision(body="op.add_column('tasks', sa.Column('n', sa.Text()))")
    )
    git("add", ".")
    git("commit", "-m", "addition")
    git("tag", "v2")
    # Uncommitted breaking code is not part of the tested release.
    (versions / "b.py").write_text(revision(body="op.drop_table('tasks')"))
    assert (await inspect_range(tmp_path, "v1", "v2"))["success"]
    (versions / "c.py").write_text(revision("c"))
    git("add", ".")
    git("commit", "-m", "sibling")
    with pytest.raises(ValueError, match="multiple.*heads"):
        await inspect_range(tmp_path, "v1", "HEAD")
