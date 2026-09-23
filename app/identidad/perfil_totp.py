"""Enrolamiento del segundo factor -- ruta JSON de la API.

`POST /api/v1/perfil/totp` inicia el enrolamiento, `POST .../confirmar` lo activa y
`DELETE /api/v1/perfil/totp` lo deshabilita. Los tres exigen sesion.

La lógica de negocio vive en `app.identidad.perfil_servicios`, compartida con la
pantalla de segundo factor del portal (`app.portal`) -- ver AD-11.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.identidad import perfil_servicios
from app.identidad.dependencias import ciudadano_actual
from app.models import Ciudadano

router = APIRouter(prefix="/api/v1/perfil/totp", tags=["segundo factor"])


class RespuestaEnrolamiento(BaseModel):
    secreto: str
    uri: str


class SolicitudConfirmacion(BaseModel):
    codigo: str = Field(min_length=1)


class SolicitudDeshabilitar(BaseModel):
    password: str = Field(min_length=1)
    codigo: str = Field(min_length=1)


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


@router.post("", response_model=RespuestaEnrolamiento, status_code=201)
async def iniciar_enrolamiento(request: Request, actual: Ciudadano = Depends(ciudadano_actual)) -> RespuestaEnrolamiento:
    """Inicia el enrolamiento del segundo factor (TOTP) para el ciudadano autenticado.

    Devuelve el secreto y una URI `otpauth://` lista para generar un código QR en una
    aplicación autenticadora. El enrolamiento queda pendiente hasta confirmarlo con
    `POST /confirmar`; una nueva solicitud reemplaza cualquier enrolamiento pendiente
    anterior, e incluso si el segundo factor ya estaba habilitado, lo reinicia desde
    cero.
    """
    secreto, uri = await perfil_servicios.iniciar_enrolamiento_totp(ciudadano_id=actual.id, correlation_id=_correlation_id(request))
    return RespuestaEnrolamiento(secreto=secreto, uri=uri)


@router.post("/confirmar", status_code=204, response_model=None)
async def confirmar_enrolamiento(
    solicitud: SolicitudConfirmacion, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> None:
    """Confirma el enrolamiento del segundo factor con un código generado a partir del
    secreto entregado por `POST /perfil/totp`.

    Al confirmarse, el segundo factor queda habilitado y se exigirá en el inicio de
    sesión y en las operaciones que lo requieran. Devuelve 409 si no hay un
    enrolamiento pendiente vigente (nunca se inició, ya se confirmó, o venció el
    tiempo límite para confirmarlo), o 401 si el código es incorrecto o venció.
    """
    await perfil_servicios.confirmar_enrolamiento_totp(
        ciudadano_id=actual.id, codigo=solicitud.codigo, correlation_id=_correlation_id(request)
    )


@router.delete("", status_code=204, response_model=None)
async def deshabilitar_totp(
    solicitud: SolicitudDeshabilitar, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> None:
    """Deshabilita el segundo factor del ciudadano autenticado.

    Exige la contraseña actual y un código del segundo factor todavía vigente, para
    evitar que una sesión robada pueda desactivarlo por sí sola. Devuelve 409 si el
    segundo factor no está habilitado, 401 si la contraseña es incorrecta, o 401 si el
    código es incorrecto o venció.
    """
    await perfil_servicios.deshabilitar_totp(
        ciudadano_id=actual.id, password=solicitud.password, codigo=solicitud.codigo, correlation_id=_correlation_id(request)
    )
