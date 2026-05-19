"""notifications table

Revision ID: a1b2c3d4e5f6
Revises: 27f9e044f302
Create Date: 2026-05-15 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "27f9e044f302"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

notification_type_enum = postgresql.ENUM(
    "team_invite",
    "deployment_failed",
    "new_commit",
    "app_down",
    name="notification_type",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        "team_invite",
        "deployment_failed",
        "new_commit",
        "app_down",
        name="notification_type",
    ).create(bind, checkfirst=True)

    inspector = sa.inspect(bind)
    if "notification" in inspector.get_table_names():
        return

    op.create_table(
        "notification",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("type", notification_type_enum, nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("body", sa.String(500), nullable=True),
        sa.Column("action_url", sa.String(2048), nullable=True),
        sa.Column("action_label", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("dedupe_key", sa.String(255), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_notification_user_id", "notification", ["user_id"])
    op.create_index("ix_notification_created_at", "notification", ["created_at"])
    op.create_index(
        "ix_notification_user_unread",
        "notification",
        ["user_id", "read_at"],
    )
    op.create_index(
        "uq_notification_user_dedupe",
        "notification",
        ["user_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "notification" not in inspector.get_table_names():
        postgresql.ENUM(name="notification_type").drop(bind, checkfirst=True)
        return

    op.drop_index("uq_notification_user_dedupe", table_name="notification")
    op.drop_index("ix_notification_user_unread", table_name="notification")
    op.drop_index("ix_notification_created_at", table_name="notification")
    op.drop_index("ix_notification_user_id", table_name="notification")
    op.drop_table("notification")
    postgresql.ENUM(name="notification_type").drop(bind, checkfirst=True)
