"""Establecimiento de la contrasena de primer acceso, y su reenvio.

Un ciudadano recibido por transferencia (CU-16) llega sin `password_hash`: no puede
iniciar sesion hasta fijar una contrasena. `app.interoperabilidad.outbox` genera el
token de un solo uso al completar la recepcion (si la transferencia trajo
`contactEmail`) y lo envia por `app.notificaciones.correo`; `POST /primer-acceso` es la
ruta que lo consume, y `POST /primer-acceso/reenviar` la que pide uno nuevo si el
primero se vencio.

Ninguna de las dos exige sesion -- el ciudadano todavia no puede autenticarse -- y por
eso ninguna revela, en ningun caso de fallo, si el token o la cedula corresponden a
alguien real: token inexistente, ya usado y vencido devuelven exactamente la misma
respuesta en `/primer-acceso`; `/primer-acceso/reenviar` responde igual sin importar si
el ciudadano existe o si esta pendiente de primer acceso.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.config import get_config
from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.identidad.dependencias import resolver_usuario
from app.identidad.seguridad import hash_password, validar_formato_password
from app.identidad.token_acceso import generar_token, hash_token
from app.models import Auditoria, Ciudadano

router = APIRouter(prefix="/api/v1", tags=["ciudadano"])

ACCIONES_REENVIO = (
    "primer_acceso.reenvio_atendido",
    "primer_acceso.reenvio_ignorado",
    "primer_acceso.reenvio_limitado",
)


class SolicitudPrimerAcceso(BaseModel):
    token: str = Field(min_length=1)
    password: str

    @field_validator("password")
    @classmethod
    def _validar_password(cls, valor: str) -> str:
        return validar_formato_password(valor)


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _origen(request: Request) -> str:
    return request.client.host if request.client else "desconocido"


@router.post("/primer-acceso", status_code=204, response_model=None)
async def establecer_password_primer_acceso(solicitud: SolicitudPrimerAcceso, request: Request) -> None:
    """Establece la contrasena inicial de un ciudadano recibido por transferencia,
    usando el token de un solo uso enviado a su correo personal al completarse la
    recepción.

    Aplica las mismas reglas de contraseña que el registro (mínimo 10 caracteres, con
    al menos una letra y un dígito). Al completarse, el ciudadano puede iniciar sesión
    normalmente y el token deja de ser válido.

    Devuelve 400 si el token no existe, ya se usó o venció — sin distinguir el motivo,
    para no revelar si corresponde a una cédula real.
    """
    correlation_id = _correlation_id(request)
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
            raise ErrorDeNegocio("TOKEN_INVALIDO", "El enlace no es valido o ya vencio.")

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
            # Misma respuesta que "token inexistente": ver docstring del modulo.
            raise ErrorDeNegocio("TOKEN_INVALIDO", "El enlace no es valido o ya vencio.")

        ciudadano.password_hash = hash_password(solicitud.password)
        # Se invalida de inmediato: un segundo intento con el mismo token, valido o no,
        # ya no encuentra ninguna fila (queda indistinguible de "token inexistente").
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


class SolicitudReenvioPrimerAcceso(BaseModel):
    usuario: str = Field(min_length=1)


class RespuestaReenvioPrimerAcceso(BaseModel):
    # Fijo, siempre el mismo texto exista o no el ciudadano: ver docstring del modulo.
    mensaje: str = (
        "Si existe una carpeta pendiente de primer acceso para esos datos, "
        "se envio un enlace a su correo personal."
    )


async def _solicitudes_recientes(
    session, *, ciudadano_id: int | None, origen: str, desde: datetime
) -> tuple[int, int]:
    """(solicitudes por cedula, solicitudes por origen) en la ventana [desde, ahora),
    contando las tres acciones de reenvio -- atendida, ignorada o limitada -- para que
    alguien enumerando cedulas al azar (que siempre caen en "ignorada") no escape del
    limite por origen. Mismo patron que `sesion._intentos_fallidos`."""
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


@router.post("/primer-acceso/reenviar", response_model=RespuestaReenvioPrimerAcceso, status_code=202)
async def reenviar_primer_acceso(
    solicitud: SolicitudReenvioPrimerAcceso, request: Request
) -> RespuestaReenvioPrimerAcceso:
    """Solicita un nuevo enlace de primer acceso, si el anterior venció.

    `usuario` acepta la cédula o la dirección de carpeta (`email_carpeta`), igual que
    el inicio de sesión. La respuesta es siempre la misma, exista o no una carpeta con
    esos datos y esté o no pendiente de primer acceso: nunca confirma ni descarta nada
    por su cuenta. Si de verdad hay una carpeta pendiente con correo personal
    registrado, se genera un enlace nuevo (el anterior deja de servir) y se envía;
    cualquier otro caso no tiene efecto.

    Devuelve 429 si se superó el número de solicitudes permitidas en la ventana
    reciente, por cédula o por origen.
    """
    cfg = get_config()
    origen = _origen(request)
    correlation_id = _correlation_id(request)

    async with SessionLocal() as session:
        ciudadano = await resolver_usuario(session, solicitud.usuario)
        ciudadano_id = ciudadano.id if ciudadano is not None else None

        desde = datetime.now(timezone.utc) - timedelta(minutes=cfg.primer_acceso_reenvio_ventana_minutos)
        por_cedula, por_origen = await _solicitudes_recientes(
            session, ciudadano_id=ciudadano_id, origen=origen, desde=desde
        )
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
            raise ErrorDeNegocio("LIMITE_DE_TASA", "Demasiadas solicitudes. Intenta de nuevo mas tarde.")

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
            return RespuestaReenvioPrimerAcceso()

        if not ciudadano.email_personal:
            # Igual que en la recepcion original (CU-16): sin canal, no se inventa uno.
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
            return RespuestaReenvioPrimerAcceso()

        from app.notificaciones.correo import enviar_correo

        token, token_hash = generar_token()
        vence_en = datetime.now(timezone.utc) + timedelta(hours=cfg.primer_acceso_token_ttl_horas)
        # Sobrescribe la fila existente: el token anterior (si habia uno vencido)
        # deja de existir en la base y cualquier intento de usarlo se vuelve
        # indistinguible de "token inexistente" -- no hace falta borrarlo aparte.
        ciudadano.token_primer_acceso_hash = token_hash
        ciudadano.token_primer_acceso_vence_en = vence_en

        await enviar_correo(
            session,
            ciudadano_id=ciudadano.id,
            destinatario=ciudadano.email_personal,
            asunto="Nuevo enlace para establecer tu contrasena en ColCarpeta",
            cuerpo=(
                f"Hola {ciudadano.nombre},\n\n"
                "Pediste un nuevo enlace para establecer tu contrasena. El anterior ya "
                f"no es valido. Este codigo es valido por {cfg.primer_acceso_token_ttl_horas} horas:\n\n"
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

    return RespuestaReenvioPrimerAcceso()
