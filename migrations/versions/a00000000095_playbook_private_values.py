"""Authenticated private playbook values, retained independently of runs."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000095"
down_revision = "a00000000094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("playbook_private_values"):
        op.create_table(
            "playbook_private_values",
            sa.Column("value_id", sa.Text(), primary_key=True),
            sa.Column("owner_kind", sa.Text(), nullable=False),
            sa.Column("owner_id", sa.Text(), nullable=False),
            sa.Column("scope_kind", sa.Text(), nullable=False),
            sa.Column("scope_owner", sa.Text(), nullable=False),
            sa.Column("format_version", sa.Integer(), nullable=False),
            sa.Column("content_type", sa.Text(), nullable=False),
            sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
            sa.Column("key_id", sa.Text(), nullable=False),
            sa.Column("nonce", sa.LargeBinary(), nullable=False),
            sa.Column("digest", sa.Text(), nullable=False),
            sa.Column("byte_count", sa.BigInteger(), nullable=False),
            sa.Column("expires_at", sa.Float()),
            sa.CheckConstraint(
                "owner_kind IN ('run', 'namespace')", name="ck_playbook_private_values_owner_kind"
            ),
            sa.CheckConstraint(
                "scope_kind IN ('system', 'project', 'profile')",
                name="ck_playbook_private_values_scope_kind",
            ),
            sa.CheckConstraint("byte_count >= 0", name="ck_playbook_private_values_byte_count"),
            sa.CheckConstraint(
                "format_version > 0", name="ck_playbook_private_values_format_version"
            ),
            sa.CheckConstraint("octet_length(nonce) = 12", name="ck_playbook_private_values_nonce"),
            sa.CheckConstraint(
                "owner_id <> '' AND scope_owner <> ''", name="ck_playbook_private_values_owner"
            ),
        )
    indexes = {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes("playbook_private_values")
    }
    for name, columns in (
        ("idx_private_values_owner", ["owner_kind", "owner_id"]),
        ("idx_private_values_expiry", ["expires_at"]),
    ):
        if name not in indexes:
            op.create_index(name, "playbook_private_values", columns)


def downgrade() -> None:
    # Rollback disables new admission; encrypted run/state evidence is retained.
    pass
