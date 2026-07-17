"""run_snapshots

Append-only per-incident checkpoint log. ``load(incident_id)`` returns the row
with the highest ``version`` for that incident; ``write`` appends the next row.

Revision ID: c362fc714309
Revises:
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c362fc714309"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("run_state", postgresql.JSONB, nullable=False),
        sa.Column(
            "written_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("incident_id", "version", name="uq_run_snapshots_incident_version"),
    )
    op.create_index(
        "idx_run_snapshots_incident_version",
        "run_snapshots",
        ["incident_id", sa.text("version DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_run_snapshots_incident_version", table_name="run_snapshots")
    op.drop_table("run_snapshots")
