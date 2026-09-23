"""Capa de servicios del primer acceso: la lógica de negocio de
`app.identidad.primer_acceso` (establecer contraseña con el token, y su reenvío),
compartida con las pantallas del portal (`app.portal`). Ver AD-11
(docs/especificacion.md).

Ninguna de las dos operaciones exige sesión -- el ciudadano todavía no puede
autenticarse -- y por eso ninguna revela, en ningún caso de fallo, si el token o la
cédula corresponden a alguien real: mismo comportamiento que la ruta JSON, solo que la
capa de transporte (JSON vs. HTML) vive en cada lado por separado.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.config import get_config
from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.identidad.dependencias import resolver_usuario
from app.identidad.seguridad import hash_password, validar_formato_password
from app.identidad.token_acceso import generar_token, hash_token
from app.models import Auditoria, Ciudadano

ACCIONES_REENVIO = (
    "primer_acceso.reenvio_atendido",
    "primer_acceso.reenvio_ignorado",
    "primer_acceso.reenvio_limitado",
)

MENSAJE_REENVIO = "Si existe una carpeta pendiente de primer acceso para esos datos, se envió un enlace a su correo personal."


class SolicitudPrimerAcceso(BaseModel):
    token: str = Field(min_length=1)
    password: str

    @field_validator("password")
    @classmethod
    def _validar_password(cls, valor: str) -> str:
        return validar_formato_password(valor)


class SolicitudReenvioPrimerAcceso(BaseModel):
    usuario: str = Field(min_length=1)


async def establecer_password(solicitud: SolicitudPrimerAcceso, *, correlation_id: str | None) -> None:
    """Ver docs/especificacion.md y el docstring original de
    `POST /api/v1/primer-acceso`: responde siempre el mismo error (`TOKEN_INVALIDO`)
    sin distinguir "no existe", "ya se usó" o "venció"."""
    token_hash = hash_token(solicitud.token)

    async with SessionLocal() as session:
        ciudadano = (
            await session.execute(select(Ciudadano).where(Ciudadano.token_primer_acceso_hash == token_hash))
        ).scalar_one_or_none()

        if ciudadano is None:
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="primer_acceso.token_invalido",
                    recurso=None,
                    ciudadano_id=None,
                    correlation_id=correlation_id,
                    detalle={},
                )
            )
            await session.commit()
            raise ErrorDeNegocio("TOKEN_INVALIDO", "El enlace no es válido o ya venció.")

        vencido = (
            ciudadano.token_primer_acceso_vence_en is None
            or ciudadano.token_primer_acceso_vence_en <= datetime.now(timezone.utc)
        )
        if vencido:
            session.add(
                Auditoria(
                    actor=str(ciudadano.id),
                    accion="primer_acceso.token_vencido",
                    recurso=str(ciudadano.id),
                    ciudadano_id=ciudadano.id,
                    correlation_id=correlation_id,
                    detalle={},
                )
            )
            await session.commit()
            raise ErrorDeNegocio("TOKEN_INVALIDO", "El enlace no es válido o ya venció.")

        ciudadano.password_hash = hash_password(solicitud.password)
        ciudadano.token_primer_acceso_hash = None
        ciudadano.token_primer_acceso_vence_en = None

        session.add(
            Auditoria(
                actor=str(ciudadano.id),
                accion="primer_acceso.token_usado",
                recurso=str(ciudadano.id),
                ciudadano_id=ciudadano.id,
                correlation_id=correlation_id,
                detalle={},
            )
        )
        await session.commit()


async def _solicitudes_recientes(session, *, ciudadano_id: int | None, origen: str, desde: datetime) -> tuple[int, int]:
    por_cedula = 0
    if ciudadano_id is not None:
        r = await session.execute(
            select(func.count()).where(
                Auditoria.accion.in_(ACCIONES_REENVIO),
                Auditoria.momento >= desde,
                Auditoria.ciudadano_id == ciudadano_id,
            )
        )
        por_cedula = r.scalar_one()

    r2 = await session.execute(
        select(func.count()).where(
            Auditoria.accion.in_(ACCIONES_REENVIO),
            Auditoria.momento >= desde,
            Auditoria.detalle["origen"].astext == origen,
        )
    )
    por_origen = r2.scalar_one()
    return por_cedula, por_origen


async def reenviar_primer_acceso(*, usuario: str, origen: str, correlation_id: str | None) -> None:
    """Nunca devuelve nada que distinga los casos entre sí -- el llamador siempre
    muestra `MENSAJE_REENVIO`, exista o no la carpeta. Solo `ErrorDeNegocio` con
    `LIMITE_DE_TASA` es visible desde afuera."""
    cfg = get_config()

    async with SessionLocal() as session:
        ciudadano = await resolver_usuario(session, usuario)
        ciudadano_id = ciudadano.id if ciudadano is not None else None

        desde = datetime.now(timezone.utc) - timedelta(minutes=cfg.primer_acceso_reenvio_ventana_minutos)
        por_cedula, por_origen = await _solicitudes_recientes(session, ciudadano_id=ciudadano_id, origen=origen, desde=desde)
        if por_cedula >= cfg.primer_acceso_reenvio_maximo or por_origen >= cfg.primer_acceso_reenvio_maximo:
            session.add(
                Auditoria(
                    actor="sistema" if ciudadano is None else str(ciudadano.id),
                    accion="primer_acceso.reenvio_limitado",
                    recurso=str(ciudadano_id) if ciudadano_id is not None else None,
                    ciudadano_id=ciudadano_id,
                    correlation_id=correlation_id,
                    detalle={"origen": origen, "intentos_cedula": por_cedula, "intentos_origen": por_origen},
                )
            )
            await session.commit()
            raise ErrorDeNegocio("LIMITE_DE_TASA", "Demasiadas solicitudes. Intenta de nuevo más tarde.")

        pendiente = ciudadano is not None and ciudadano.password_hash is None
        if not pendiente:
            session.add(
                Auditoria(
                    actor="sistema" if ciudadano is None else str(ciudadano.id),
                    accion="primer_acceso.reenvio_ignorado",
                    recurso=str(ciudadano_id) if ciudadano_id is not None else None,
                    ciudadano_id=ciudadano_id,
                    correlation_id=correlation_id,
                    detalle={"origen": origen},
                )
            )
            await session.commit()
            return

        if not ciudadano.email_personal:
            session.add(
                Auditoria(
                    actor=str(ciudadano.id),
                    accion="primer_acceso.reenvio_ignorado",
                    recurso=str(ciudadano.id),
                    ciudadano_id=ciudadano.id,
                    correlation_id=correlation_id,
                    detalle={"origen": origen, "motivo": "sin contactEmail registrado"},
                )
            )
            await session.commit()
            return

        from app.notificaciones.correo import enviar_correo

        token, token_hash = generar_token()
        vence_en = datetime.now(timezone.utc) + timedelta(hours=cfg.primer_acceso_token_ttl_horas)
        ciudadano.token_primer_acceso_hash = token_hash
        ciudadano.token_primer_acceso_vence_en = vence_en

        await enviar_correo(
            session,
            ciudadano_id=ciudadano.id,
            destinatario=ciudadano.email_personal,
            asunto="Nuevo enlace para establecer tu contraseña en ColCarpeta",
            cuerpo=(
                f"Hola {ciudadano.nombre},\n\n"
                "Pediste un nuevo enlace para establecer tu contraseña. El anterior ya "
                f"no es válido. Este código es válido por {cfg.primer_acceso_token_ttl_horas} horas:\n\n"
                f"{token}\n"
            ),
        )
        session.add(
            Auditoria(
                actor=str(ciudadano.id),
                accion="primer_acceso.reenvio_atendido",
                recurso=str(ciudadano.id),
                ciudadano_id=ciudadano.id,
                correlation_id=correlation_id,
                detalle={"origen": origen, "enviado_a": ciudadano.email_personal, "vence_en": vence_en.isoformat()},
            )
        )
        await session.commit()
