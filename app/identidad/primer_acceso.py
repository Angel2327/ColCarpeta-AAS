"""Establecimiento de la contrasena de primer acceso, y su reenvio -- ruta JSON de la
API.

Un ciudadano recibido por transferencia (CU-16) llega sin `password_hash`: no puede
iniciar sesion hasta fijar una contrasena. `app.interoperabilidad.outbox` genera el
token de un solo uso al completar la recepcion (si la transferencia trajo
`contactEmail`) y lo envia por `app.notificaciones.correo`; `POST /primer-acceso` es la
ruta que lo consume, y `POST /primer-acceso/reenviar` la que pide uno nuevo si el
primero se vencio.

La lógica de negocio vive en `app.identidad.primer_acceso_servicios`, compartida con
las pantallas del portal (`app.portal`) -- ver AD-11 (docs/especificacion.md).
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.identidad.primer_acceso_servicios import (
    MENSAJE_REENVIO,
    SolicitudPrimerAcceso,
    SolicitudReenvioPrimerAcceso,
    establecer_password,
    reenviar_primer_acceso,
)
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1", tags=["ciudadano"])


class RespuestaReenvioPrimerAcceso(BaseModel):
    mensaje: str = MENSAJE_REENVIO


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
    await establecer_password(solicitud, correlation_id=_correlation_id(request))


@router.post("/primer-acceso/reenviar", response_model=RespuestaReenvioPrimerAcceso, status_code=202)
async def reenviar(solicitud: SolicitudReenvioPrimerAcceso, request: Request) -> RespuestaReenvioPrimerAcceso:
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
    await reenviar_primer_acceso(usuario=solicitud.usuario, origen=_origen(request), correlation_id=_correlation_id(request))
    return RespuestaReenvioPrimerAcceso()
