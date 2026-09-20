"""estado totp del ciudadano

Revision ID: e2a9cbf3dff8
Revises: 7c93150cf0b6
Create Date: 2026-09-19 19:01:11.948125
"""
from alembic import op
import sqlalchemy as sa


revision = 'e2a9cbf3dff8'
down_revision = '7c93150cf0b6'
branch_labels = None
depends_on = None

# A diferencia de CREATE TABLE, ALTER TABLE ADD COLUMN no crea el tipo ENUM de Postgres
# por si solo: hay que crearlo (y borrarlo) explicitamente.
estado_totp = sa.Enum("PENDIENTE", "HABILITADO", name="estado_totp")


def upgrade() -> None:
    estado_totp.create(op.get_bind(), checkfirst=True)
    op.add_column('ciudadano', sa.Column('totp_estado', estado_totp, nullable=True))
    op.add_column('ciudadano', sa.Column('totp_secret_actualizado_en', sa.DateTime(timezone=True), nullable=True))
    op.add_column('ciudadano', sa.Column('totp_ultimo_paso', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('ciudadano', 'totp_ultimo_paso')
    op.drop_column('ciudadano', 'totp_secret_actualizado_en')
    op.drop_column('ciudadano', 'totp_estado')
    estado_totp.drop(op.get_bind(), checkfirst=True)
