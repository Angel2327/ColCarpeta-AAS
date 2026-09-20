"""Modelos de datos.

Ver docs/especificacion.md, seccion "Modelo de datos", para el detalle de campos,
estados y cardinalidades. Tablas: ciudadano, documento, outbox, transferencia,
operador_cache, autorizacion, auditoria.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum as PgEnum, ForeignKey, Integer, String, Text, Uuid, event, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class EstadoCiudadano(str, enum.Enum):
    PENDIENTE_VERIFICACION = "PENDIENTE_VERIFICACION"
    PENDIENTE_CENTRALIZADOR = "PENDIENTE_CENTRALIZADOR"
    ACTIVO = "ACTIVO"
    EN_TRANSFERENCIA = "EN_TRANSFERENCIA"
    TRASLADADO = "TRASLADADO"


class EstadoAutenticacionDocumento(str, enum.Enum):
    NO_SOLICITADA = "NO_SOLICITADA"
    PENDIENTE = "PENDIENTE"
    AUTENTICADO = "AUTENTICADO"
    RECHAZADO = "RECHAZADO"


class EstadoOutbox(str, enum.Enum):
    PENDIENTE = "PENDIENTE"
    EN_PROCESO = "EN_PROCESO"
    COMPLETADO = "COMPLETADO"
    FALLIDO = "FALLIDO"


class EstadoTransferencia(str, enum.Enum):
    ENVIADA = "ENVIADA"
    CONFIRMADA = "CONFIRMADA"
    PURGADA = "PURGADA"
    FALLIDA = "FALLIDA"


class EstadoTotp(str, enum.Enum):
    PENDIENTE = "PENDIENTE"
    HABILITADO = "HABILITADO"


class Ciudadano(Base):
    __tablename__ = "ciudadano"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    nombre: Mapped[str] = mapped_column(String(255))
    direccion: Mapped[str] = mapped_column(String(500))
    # Nulo mientras el ciudadano esta en PENDIENTE_VERIFICACION: el correo se genera
    # despues de confirmar la identidad (CU-01, paso 5).
    email_carpeta: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    email_personal: Mapped[str] = mapped_column(String(255))
    telefono: Mapped[str] = mapped_column(String(30))
    password_hash: Mapped[str] = mapped_column(String(255))
    # totp_secret + totp_estado siguen el enrolamiento (docs/especificacion.md, "Segundo
    # factor"): NULL/sin totp_estado = nunca enrolado. totp_secret_actualizado_en fija el
    # vencimiento de 15 min de un secreto PENDIENTE. totp_ultimo_paso evita reusar un
    # codigo dentro de su periodo (solo aplica en modo TOTP_MODO=real).
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_estado: Mapped[EstadoTotp | None] = mapped_column(PgEnum(EstadoTotp, name="estado_totp"))
    totp_secret_actualizado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    totp_ultimo_paso: Mapped[int | None] = mapped_column(BigInteger)
    estado: Mapped[EstadoCiudadano] = mapped_column(
        PgEnum(EstadoCiudadano, name="estado_ciudadano"),
        default=EstadoCiudadano.PENDIENTE_VERIFICACION,
    )
    identidad_verificada: Mapped[bool] = mapped_column(Boolean, default=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    documentos: Mapped[list["Documento"]] = relationship(back_populates="ciudadano")
    transferencias: Mapped[list["Transferencia"]] = relationship(back_populates="ciudadano")
    auditorias: Mapped[list["Auditoria"]] = relationship(back_populates="ciudadano")


class Documento(Base):
    __tablename__ = "documento"

    # UUID, no serial: el contrato de la API (POST /api/v1/documentos) expone el id como
    # cadena opaca, no como entero secuencial (evita enumerar documentos de otros).
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ciudadano_id: Mapped[int] = mapped_column(ForeignKey("ciudadano.id"), index=True)
    titulo: Mapped[str] = mapped_column(String(255))
    tipo: Mapped[str] = mapped_column(String(100))
    entidad_emisora: Mapped[str | None] = mapped_column(String(255))
    fecha_emision: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    s3_key: Mapped[str] = mapped_column(String(512), unique=True)
    content_type: Mapped[str] = mapped_column(String(100))
    tamano_bytes: Mapped[int] = mapped_column(BigInteger)
    hash_sha256: Mapped[str] = mapped_column(String(64))
    certificado: Mapped[bool] = mapped_column(Boolean, default=False)
    estado_autenticacion: Mapped[EstadoAutenticacionDocumento] = mapped_column(
        PgEnum(EstadoAutenticacionDocumento, name="estado_autenticacion_documento"),
        default=EstadoAutenticacionDocumento.NO_SOLICITADA,
    )
    firma_valida: Mapped[bool | None] = mapped_column(Boolean)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ciudadano: Mapped["Ciudadano"] = relationship(back_populates="documentos")
    autorizaciones: Mapped[list["Autorizacion"]] = relationship(back_populates="documento")


class Outbox(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    operacion: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)
    estado: Mapped[EstadoOutbox] = mapped_column(
        PgEnum(EstadoOutbox, name="estado_outbox"), default=EstadoOutbox.PENDIENTE
    )
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    proximo_intento: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ultimo_error: Mapped[str | None] = mapped_column(Text)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OperadorCache(Base):
    __tablename__ = "operador_cache"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(255))
    transfer_api_url: Mapped[str | None] = mapped_column(String(500))
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    transferencias: Mapped[list["Transferencia"]] = relationship(back_populates="operador_destino")


class Transferencia(Base):
    __tablename__ = "transferencia"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ciudadano_id: Mapped[int] = mapped_column(ForeignKey("ciudadano.id"), index=True)
    operador_destino_id: Mapped[str] = mapped_column(ForeignKey("operador_cache.id"), index=True)
    confirm_api: Mapped[str] = mapped_column(String(500))
    estado: Mapped[EstadoTransferencia] = mapped_column(
        PgEnum(EstadoTransferencia, name="estado_transferencia"), default=EstadoTransferencia.ENVIADA
    )
    enviada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purgar_despues_de: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ciudadano: Mapped["Ciudadano"] = relationship(back_populates="transferencias")
    operador_destino: Mapped["OperadorCache"] = relationship(back_populates="transferencias")


class Autorizacion(Base):
    __tablename__ = "autorizacion"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    documento_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documento.id"), index=True)
    tercero: Mapped[str] = mapped_column(String(255))
    otorgada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    vence_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    documento: Mapped["Documento"] = relationship(back_populates="autorizaciones")


class Auditoria(Base):
    """De solo insercion: no se define UPDATE ni DELETE (CLAUDE.md, especificacion.md)."""

    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    momento: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    accion: Mapped[str] = mapped_column(String(100))
    recurso: Mapped[str | None] = mapped_column(String(255))
    correlation_id: Mapped[str | None] = mapped_column(String(32), index=True)
    detalle: Mapped[dict | None] = mapped_column(JSONB)
    # Nullable a proposito: los eventos sin ciudadano asociado quedan solo con `actor`
    # (p. ej. "sistema"). `actor` sigue siendo texto libre, no se reemplaza por esta FK.
    # ON DELETE SET NULL: la auditoria debe sobrevivir al ciudadano (RNF14, retencion de
    # 5 anios) y la purga diferida no puede quedar bloqueada por sus propios eventos.
    ciudadano_id: Mapped[int | None] = mapped_column(
        ForeignKey("ciudadano.id", ondelete="SET NULL"), index=True
    )

    ciudadano: Mapped["Ciudadano | None"] = relationship(back_populates="auditorias")


@event.listens_for(Auditoria, "before_update")
def _prohibir_actualizacion_auditoria(mapper, connection, target) -> None:
    raise RuntimeError("auditoria es de solo insercion: no se permite UPDATE")


@event.listens_for(Auditoria, "before_delete")
def _prohibir_borrado_auditoria(mapper, connection, target) -> None:
    raise RuntimeError("auditoria es de solo insercion: no se permite DELETE")
