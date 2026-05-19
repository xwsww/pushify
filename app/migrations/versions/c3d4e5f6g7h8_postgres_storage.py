"""PostgreSQL storage support

Revision ID: c3d4e5f6g7h8
Revises: a1b2c3d4e5f6
Create Date: 2026-05-19 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6g7h8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


new_storage_type_enum = sa.Enum(
    "database", "mariadb", "postgres", "volume", "kv", "queue", name="storage_type_new"
)
old_storage_type_enum = sa.Enum(
    "database", "mariadb", "volume", "kv", "queue", name="storage_type_old"
)


def upgrade() -> None:
    """Upgrade schema."""
    new_storage_type_enum.create(op.get_bind(), checkfirst=True)

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


def downgrade() -> None:
    """Downgrade schema."""
    old_storage_type_enum.create(op.get_bind(), checkfirst=True)
    op.execute("ALTER TABLE storage ALTER COLUMN type DROP DEFAULT")
    op.execute(
        """
        UPDATE storage
        SET type = 'database'
        WHERE type = 'postgres'
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
