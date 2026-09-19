"""agregar ciudadano_id a auditoria

Revision ID: c6336c116d63
Revises: c1dd8f165015
Create Date: 2026-09-19 16:43:31.804207
"""
from alembic import op
import sqlalchemy as sa


revision = 'c6336c116d63'
down_revision = 'c1dd8f165015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("auditoria", sa.Column("ciudadano_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_auditoria_ciudadano_id", "auditoria", ["ciudadano_id"], unique=False)
    op.create_foreign_key(
        "fk_auditoria_ciudadano_id_ciudadano",
        "auditoria",
        "ciudadano",
        ["ciudadano_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_auditoria_ciudadano_id_ciudadano", "auditoria", type_="foreignkey")
    op.drop_index("ix_auditoria_ciudadano_id", table_name="auditoria")
    op.drop_column("auditoria", "ciudadano_id")
