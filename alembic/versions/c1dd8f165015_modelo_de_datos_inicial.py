"""modelo de datos inicial

Revision ID: c1dd8f165015
Revises:
Create Date: 2026-09-19 16:04:49.184087
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'c1dd8f165015'
down_revision = None
branch_labels = None
depends_on = None


estado_ciudadano = sa.Enum(
    "PENDIENTE_VERIFICACION",
    "PENDIENTE_CENTRALIZADOR",
    "ACTIVO",
    "EN_TRANSFERENCIA",
    "TRASLADADO",
    name="estado_ciudadano",
)
estado_autenticacion_documento = sa.Enum(
    "NO_SOLICITADA",
    "PENDIENTE",
    "AUTENTICADO",
    "RECHAZADO",
    name="estado_autenticacion_documento",
)
estado_outbox = sa.Enum(
    "PENDIENTE",
    "EN_PROCESO",
    "COMPLETADO",
    "FALLIDO",
    name="estado_outbox",
)
estado_transferencia = sa.Enum(
    "ENVIADA",
    "CONFIRMADA",
    "PURGADA",
    "FALLIDA",
    name="estado_transferencia",
)


def upgrade() -> None:
    op.create_table(
        "ciudadano",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("nombre", sa.String(length=255), nullable=False),
        sa.Column("direccion", sa.String(length=500), nullable=False),
        sa.Column("email_carpeta", sa.String(length=255), nullable=False),
        sa.Column("email_personal", sa.String(length=255), nullable=False),
        sa.Column("telefono", sa.String(length=30), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("totp_secret", sa.String(length=64), nullable=True),
        sa.Column("estado", estado_ciudadano, nullable=False),
        sa.Column("identidad_verificada", sa.Boolean(), nullable=False),
        sa.Column("creado_en", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ciudadano_email_carpeta", "ciudadano", ["email_carpeta"], unique=True)

    op.create_table(
        "operador_cache",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("nombre", sa.String(length=255), nullable=False),
        sa.Column("transfer_api_url", sa.String(length=500), nullable=True),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "documento",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ciudadano_id", sa.BigInteger(), nullable=False),
        sa.Column("titulo", sa.String(length=255), nullable=False),
        sa.Column("tipo", sa.String(length=100), nullable=False),
        sa.Column("entidad_emisora", sa.String(length=255), nullable=True),
        sa.Column("fecha_emision", sa.DateTime(timezone=True), nullable=True),
        sa.Column("s3_key", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("tamano_bytes", sa.BigInteger(), nullable=False),
        sa.Column("hash_sha256", sa.String(length=64), nullable=False),
        sa.Column("certificado", sa.Boolean(), nullable=False),
        sa.Column("estado_autenticacion", estado_autenticacion_documento, nullable=False),
        sa.Column("firma_valida", sa.Boolean(), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["ciudadano_id"], ["ciudadano.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("s3_key"),
    )
    op.create_index("ix_documento_ciudadano_id", "documento", ["ciudadano_id"], unique=False)

    op.create_table(
        "transferencia",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ciudadano_id", sa.BigInteger(), nullable=False),
        sa.Column("operador_destino_id", sa.String(length=64), nullable=False),
        sa.Column("confirm_api", sa.String(length=500), nullable=False),
        sa.Column("estado", estado_transferencia, nullable=False),
        sa.Column("enviada_en", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("confirmada_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purgar_despues_de", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["ciudadano_id"], ["ciudadano.id"]),
        sa.ForeignKeyConstraint(["operador_destino_id"], ["operador_cache.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transferencia_ciudadano_id", "transferencia", ["ciudadano_id"], unique=False)
    op.create_index(
        "ix_transferencia_operador_destino_id", "transferencia", ["operador_destino_id"], unique=False
    )

    op.create_table(
        "autorizacion",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("documento_id", sa.BigInteger(), nullable=False),
        sa.Column("tercero", sa.String(length=255), nullable=False),
        sa.Column("otorgada_en", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("vence_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocada_en", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["documento_id"], ["documento.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_autorizacion_documento_id", "autorizacion", ["documento_id"], unique=False)

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("operacion", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("estado", estado_outbox, nullable=False),
        sa.Column("intentos", sa.Integer(), nullable=False),
        sa.Column("proximo_intento", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ultimo_error", sa.Text(), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "auditoria",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("momento", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("accion", sa.String(length=100), nullable=False),
        sa.Column("recurso", sa.String(length=255), nullable=True),
        sa.Column("correlation_id", sa.String(length=32), nullable=True),
        sa.Column("detalle", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auditoria_momento", "auditoria", ["momento"], unique=False)
    op.create_index("ix_auditoria_correlation_id", "auditoria", ["correlation_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_auditoria_correlation_id", table_name="auditoria")
    op.drop_index("ix_auditoria_momento", table_name="auditoria")
    op.drop_table("auditoria")

    op.drop_table("outbox")

    op.drop_index("ix_autorizacion_documento_id", table_name="autorizacion")
    op.drop_table("autorizacion")

    op.drop_index("ix_transferencia_operador_destino_id", table_name="transferencia")
    op.drop_index("ix_transferencia_ciudadano_id", table_name="transferencia")
    op.drop_table("transferencia")

    op.drop_index("ix_documento_ciudadano_id", table_name="documento")
    op.drop_table("documento")

    op.drop_table("operador_cache")

    op.drop_index("ix_ciudadano_email_carpeta", table_name="ciudadano")
    op.drop_table("ciudadano")

    # op.drop_table no elimina el tipo ENUM de Postgres asociado: hay que borrarlo aparte.
    estado_outbox.drop(op.get_bind(), checkfirst=True)
    estado_transferencia.drop(op.get_bind(), checkfirst=True)
    estado_autenticacion_documento.drop(op.get_bind(), checkfirst=True)
    estado_ciudadano.drop(op.get_bind(), checkfirst=True)
