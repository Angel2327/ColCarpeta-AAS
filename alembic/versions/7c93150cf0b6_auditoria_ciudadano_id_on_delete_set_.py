"""auditoria ciudadano_id on delete set null

Revision ID: 7c93150cf0b6
Revises: de850422e354
Create Date: 2026-09-19 18:10:23.574187
"""
from alembic import op
import sqlalchemy as sa


revision = '7c93150cf0b6'
down_revision = 'de850422e354'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint('fk_auditoria_ciudadano_id_ciudadano', 'auditoria', type_='foreignkey')
    op.create_foreign_key(
        'fk_auditoria_ciudadano_id_ciudadano',
        'auditoria',
        'ciudadano',
        ['ciudadano_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_auditoria_ciudadano_id_ciudadano', 'auditoria', type_='foreignkey')
    op.create_foreign_key(
        'fk_auditoria_ciudadano_id_ciudadano', 'auditoria', 'ciudadano', ['ciudadano_id'], ['id']
    )
