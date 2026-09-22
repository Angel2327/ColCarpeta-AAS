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
    # Nulo para un ciudadano recibido por transferencia (CU-16): llega sin contrasena
    # local, la define en su primer inicio de sesion. verificar_password() trata NULL
    # como "no puede autenticar con contrasena", nunca como coincidencia.
    password_hash: Mapped[str | None] = mapped_column(String(255))
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

    # passive_deletes=True: mismo motivo que en `auditorias` mas abajo. documento.ciudadano_id
    # no admite NULL, asi que sin esto, al borrar un ciudadano con documentos ya borrados
    # explicitamente en la misma transaccion (p. ej. el descarte de CU-16 en
    # app.interoperabilidad.outbox), SQLAlchemy igual recarga esta coleccion para nulificarla
    # antes de borrar el ciudadano y choca contra esa restriccion. Con passive_deletes=True
    # no la toca: confia en que quien borra el ciudadano ya elimino sus documentos.
    documentos: Mapped[list["Documento"]] = relationship(back_populates="ciudadano", passive_deletes=True)
    # passive_deletes=True: mismo motivo que auditorias -- la purga fisica (CU-03) borra
    # al ciudadano pero conserva sus filas de transferencia (ciudadano_id nullable, ON
    # DELETE SET NULL). Sin esto, SQLAlchemy intentaria nulificarlas el mismo con un
    # UPDATE de ORM antes del DELETE en vez de dejar que lo haga la FK de Postgres.
    transferencias: Mapped[list["Transferencia"]] = relationship(back_populates="ciudadano", passive_deletes=True)
    # passive_deletes=True: sin esto, al borrar un ciudadano SQLAlchemy nulifica
    # ciudadano_id en cada Auditoria con un UPDATE de ORM antes del DELETE, y el listener
    # de solo-insercion de Auditoria lo rechaza. Con passive_deletes=True el ORM no toca
    # esas filas: deja que Postgres aplique el ON DELETE SET NULL de la propia FK.
    auditorias: Mapped[list["Auditoria"]] = relationship(back_populates="ciudadano", passive_deletes=True)


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
    # No estaban en la tabla de "Modelo de datos", pero CU-11 y el contrato de
    # GET .../autenticacion los necesitan: la respuesta cruda del centralizador y cuando
    # cambio estado_autenticacion por ultima vez.
    respuesta_centralizador: Mapped[str | None] = mapped_column(Text)
    autenticacion_actualizada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    # Cuando se marco EN_PROCESO por ultima vez; NULL en cualquier otro estado. Permite
    # detectar filas colgadas (el proceso que las tomo murio, se colgo, o hubo un
    # redespliegue a mitad de ejecucion) y revivirlas -- ver
    # app.interoperabilidad.outbox._recuperar_colgadas.
    tomado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # Nullable a proposito, igual que Auditoria.ciudadano_id: la purga fisica (CU-03,
    # "Borrado") borra al ciudadano pero conserva esta fila como rastro de que existio y
    # se traslado (RNF14). ON DELETE SET NULL: sin eso, Postgres rechazaria el borrado
    # del ciudadano mientras una fila de transferencia lo siga referenciando.
    ciudadano_id: Mapped[int | None] = mapped_column(ForeignKey("ciudadano.id", ondelete="SET NULL"), index=True)
    operador_destino_id: Mapped[str] = mapped_column(ForeignKey("operador_cache.id"), index=True)
    confirm_api: Mapped[str] = mapped_column(String(500))
    estado: Mapped[EstadoTransferencia] = mapped_column(
        PgEnum(EstadoTransferencia, name="estado_transferencia"), default=EstadoTransferencia.ENVIADA
    )
    enviada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purgar_despues_de: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ciudadano: Mapped["Ciudadano | None"] = relationship(back_populates="transferencias")
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
