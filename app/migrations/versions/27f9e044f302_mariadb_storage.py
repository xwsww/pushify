"""MariaDB storage support

Revision ID: 27f9e044f302
Revises: 4fe4c96ad3dd
Create Date: 2026-05-14 17:55:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "27f9e044f302"
down_revision: Union[str, Sequence[str], None] = "4fe4c96ad3dd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


new_storage_type_enum = sa.Enum(
    "database", "mariadb", "volume", "kv", "queue", name="storage_type_new"
)
old_storage_type_enum = sa.Enum(
    "database", "volume", "kv", "queue", name="storage_type_old"
)
storage_db_user_scope_enum = sa.Enum(
    "admin", "project", "custom", name="storage_db_user_scope"
)
storage_db_user_scope_existing = postgresql.ENUM(
    "admin",
    "project",
    "custom",
    name="storage_db_user_scope",
    create_type=False,
)


def upgrade() -> None:
    """Upgrade schema."""
    new_storage_type_enum.create(op.get_bind(), checkfirst=True)
    storage_db_user_scope_enum.create(op.get_bind(), checkfirst=True)

    op.execute("ALTER TABLE storage ALTER COLUMN type DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE storage
        ALTER COLUMN type TYPE storage_type_new
        USING type::text::storage_type_new
        """
    )
    op.execute("DROP TYPE storage_type")
    op.execute("ALTER TYPE storage_type_new RENAME TO storage_type")

    op.create_table(
        "storage_db_user",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("storage_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("password", sa.Text(), nullable=False),
        sa.Column(
            "scope",
            storage_db_user_scope_existing,
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL", use_alter=True),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["storage_id"], ["storage.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "storage_id",
            "username",
            name="uq_storage_db_user_storage_name",
        ),
    )
    op.create_index(
        op.f("ix_storage_db_user_project_id"),
        "storage_db_user",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_storage_db_user_storage_id"),
        "storage_db_user",
        ["storage_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_storage_db_user_storage_id"), table_name="storage_db_user")
    op.drop_index(op.f("ix_storage_db_user_project_id"), table_name="storage_db_user")
    op.drop_table("storage_db_user")
    op.execute("DROP TYPE storage_db_user_scope")

    old_storage_type_enum.create(op.get_bind(), checkfirst=True)
    op.execute("ALTER TABLE storage ALTER COLUMN type DROP DEFAULT")
    op.execute(
        """
        UPDATE storage
        SET type = 'database'
        WHERE type = 'mariadb'
        """
    )
    op.execute(
        """
        ALTER TABLE storage
        ALTER COLUMN type TYPE storage_type_old
        USING type::text::storage_type_old
        """
    )
    op.execute("DROP TYPE storage_type")
    op.execute("ALTER TYPE storage_type_old RENAME TO storage_type")
