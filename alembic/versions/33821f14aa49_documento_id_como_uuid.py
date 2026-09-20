"""documento id como uuid

Revision ID: 33821f14aa49
Revises: e2a9cbf3dff8
Create Date: 2026-09-19 19:56:04.325473

Postgres no tiene un cast implicito de bigint a uuid (ni con las tablas vacias alcanza
con `ALTER COLUMN ... TYPE uuid`, autogenerate lo intento y falla en el servidor real).
Como `documento` y `autorizacion` estan vacias en esta etapa, se recrean las columnas en
lugar de convertirlas.
"""
from alembic import op
import sqlalchemy as sa


revision = '33821f14aa49'
down_revision = 'e2a9cbf3dff8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint('autorizacion_documento_id_fkey', 'autorizacion', type_='foreignkey')
    op.drop_index('ix_autorizacion_documento_id', table_name='autorizacion')
    op.drop_column('autorizacion', 'documento_id')

    op.drop_constraint('documento_pkey', 'documento', type_='primary')
    op.drop_column('documento', 'id')
    op.add_column('documento', sa.Column('id', sa.Uuid(), nullable=False))
    op.create_primary_key('documento_pkey', 'documento', ['id'])

    op.add_column('autorizacion', sa.Column('documento_id', sa.Uuid(), nullable=False))
    op.create_index('ix_autorizacion_documento_id', 'autorizacion', ['documento_id'])
    op.create_foreign_key(
        'autorizacion_documento_id_fkey', 'autorizacion', 'documento', ['documento_id'], ['id']
    )


def downgrade() -> None:
    op.drop_constraint('autorizacion_documento_id_fkey', 'autorizacion', type_='foreignkey')
    op.drop_index('ix_autorizacion_documento_id', table_name='autorizacion')
    op.drop_column('autorizacion', 'documento_id')

    op.drop_constraint('documento_pkey', 'documento', type_='primary')
    op.drop_column('documento', 'id')
    op.add_column('documento', sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False))
    op.create_primary_key('documento_pkey', 'documento', ['id'])

    op.add_column('autorizacion', sa.Column('documento_id', sa.BigInteger(), nullable=False))
    op.create_index('ix_autorizacion_documento_id', 'autorizacion', ['documento_id'])
    op.create_foreign_key(
        'autorizacion_documento_id_fkey', 'autorizacion', 'documento', ['documento_id'], ['id']
    )
