"""CU-02: inicio y cierre de sesión -- ruta JSON de la API.

La lógica de negocio vive en `app.identidad.servicios` (`iniciar_sesion`,
`cerrar_sesion`), compartida con el formulario de inicio de sesión del portal
(`app.portal`): esta ruta solo adapta esas llamadas al formato JSON de la API y emite
el `Authorization: Bearer` que la API usa; el portal usa la misma llamada mas una
cookie -- ver AD-11 (docs/especificacion.md).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.identidad.dependencias import ciudadano_actual
from app.identidad.servicios import RespuestaSesion, SolicitudSesion, cerrar_sesion, iniciar_sesion
from app.models import Ciudadano

router = APIRouter(prefix="/api/v1", tags=["sesion"])


def _origen(request: Request) -> str:
    return request.client.host if request.client else "desconocido"


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


@router.post("/sesion", response_model=RespuestaSesion)
async def iniciar(solicitud: SolicitudSesion, request: Request) -> RespuestaSesion:
    """Inicia sesión y devuelve un token de acceso.

    `usuario` acepta la cédula o la dirección de carpeta (`email_carpeta`). Si el
    ciudadano tiene el segundo factor habilitado, la primera petición sin
    `codigo_totp` responde 428 (la contraseña ya se validó, pero no se emite token);
    se repite la petición completa incluyendo el código para obtenerlo.

    Devuelve 423 si se superó el número de intentos fallidos permitidos (por cédula o
    por origen), 401 si el usuario o la contraseña son incorrectos, 409 si la carpeta
    está en proceso de traslado o ya fue trasladada a otro operador, o 401 si el
    código del segundo factor es incorrecto o venció.
    """
    return await iniciar_sesion(solicitud, origen=_origen(request), correlation_id=_correlation_id(request))


@router.delete("/sesion", status_code=204, response_model=None)
async def cerrar(request: Request, ciudadano: Ciudadano = Depends(ciudadano_actual)) -> None:
    """Cierra la sesión actual.

    El token de acceso usado en esta petición no se invalida: sigue siendo válido
    hasta que expira por su cuenta. El cliente debe descartarlo por su lado.
    """
    await cerrar_sesion(ciudadano_id=ciudadano.id, origen=_origen(request), correlation_id=_correlation_id(request))
