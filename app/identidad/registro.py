"""CU-01: registro del ciudadano -- ruta JSON de la API.

La lógica de negocio vive en `app.identidad.servicios.registrar_ciudadano`, compartida
con la pantalla de registro del portal (`app.portal`): esta ruta solo adapta esa
llamada al formato JSON de la API. Ver ese módulo para el detalle de cada rama
(A1-A2, E1-E6) y AD-11 (docs/especificacion.md) para por qué el portal no llama a esta
ruta por HTTP en vez de compartir la función.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.identidad.servicios import RespuestaRegistro, SolicitudRegistro, registrar_ciudadano

router = APIRouter(prefix="/api/v1", tags=["ciudadano"])


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


@router.post("/registro", response_model=RespuestaRegistro, status_code=201)
async def registrar(solicitud: SolicitudRegistro, request: Request) -> RespuestaRegistro:
    """Registra un nuevo ciudadano en ColCarpeta.

    Verifica la identidad del ciudadano y genera su dirección de carpeta
    (`email_carpeta`), que es permanente y no puede cambiarse después. El estado
    devuelto puede ser `PENDIENTE_CENTRALIZADOR`: es normal justo después de
    registrarse, mientras se completa la afiliación ante el sistema nacional; la
    carpeta se considera activa cuando el estado pasa a `ACTIVO`.

    Devuelve 409 si la cédula ya está registrada en ColCarpeta o ya está afiliada a
    otro operador, 409 si no fue posible confirmar la identidad del ciudadano, o 503
    si el servicio de verificación de identidad o el sistema nacional no están
    disponibles en este momento.
    """
    return await registrar_ciudadano(solicitud, request.app, correlation_id=_correlation_id(request))
