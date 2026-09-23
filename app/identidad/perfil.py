"""Datos del ciudadano y estado de su carpeta -- ruta JSON de la API.

Ver docs/especificacion.md, "Contrato de la API propia" > "Ciudadano y sesion":
`GET /api/v1/perfil` ya estaba documentado ahi pero nunca se habia implementado.
`PATCH` es una extension propia para los datos de contacto que la especificacion no fija
como inmutables (a diferencia de la cedula y `email_carpeta`, ver AD-10).

La lógica de negocio vive en `app.identidad.perfil_servicios`, compartida con la
pantalla de perfil del portal (`app.portal`) -- ver AD-11.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field

from app.identidad import perfil_servicios
from app.identidad.dependencias import ciudadano_actual
from app.models import Ciudadano, EstadoCiudadano, EstadoTotp

router = APIRouter(prefix="/api/v1/perfil", tags=["perfil"])


class RespuestaPerfil(BaseModel):
    id: int
    nombre: str
    direccion: str
    email_carpeta: str | None
    email_personal: str
    telefono: str
    estado: EstadoCiudadano
    identidad_verificada: bool
    segundo_factor_habilitado: bool
    cuota_bytes: int
    usado_bytes: int
    creado_en: datetime


class SolicitudActualizarPerfil(BaseModel):
    direccion: str | None = Field(None, min_length=1, max_length=500)
    telefono: str | None = Field(None, min_length=1, max_length=30)
    email_personal: EmailStr | None = None


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _a_respuesta(ciudadano: Ciudadano, *, cuota_bytes: int, usado_bytes: int) -> RespuestaPerfil:
    return RespuestaPerfil(
        id=ciudadano.id,
        nombre=ciudadano.nombre,
        direccion=ciudadano.direccion,
        email_carpeta=ciudadano.email_carpeta,
        email_personal=ciudadano.email_personal,
        telefono=ciudadano.telefono,
        estado=ciudadano.estado,
        identidad_verificada=ciudadano.identidad_verificada,
        segundo_factor_habilitado=ciudadano.totp_estado == EstadoTotp.HABILITADO,
        cuota_bytes=cuota_bytes,
        usado_bytes=usado_bytes,
        creado_en=ciudadano.creado_en,
    )


@router.get("", response_model=RespuestaPerfil)
async def obtener_perfil(actual: Ciudadano = Depends(ciudadano_actual)) -> RespuestaPerfil:
    """Consulta los datos del ciudadano autenticado y el estado de su carpeta.

    Incluye la cuota de almacenamiento de documentos temporales y cuánto se ha
    consumido de ella; los documentos certificados no cuentan contra la cuota.
    """
    ciudadano, cuota_bytes, usado_bytes = await perfil_servicios.obtener_perfil(ciudadano_id=actual.id)
    return _a_respuesta(ciudadano, cuota_bytes=cuota_bytes, usado_bytes=usado_bytes)


@router.patch("", response_model=RespuestaPerfil)
async def actualizar_perfil(
    solicitud: SolicitudActualizarPerfil, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> RespuestaPerfil:
    """Actualiza los datos de contacto del ciudadano autenticado.

    Admite dirección, teléfono y correo personal; los campos que no se incluyan quedan
    sin modificar. La cédula y la dirección de carpeta (`email_carpeta`) son
    permanentes y no se pueden cambiar por esta vía.
    """
    ciudadano, cuota_bytes, usado_bytes = await perfil_servicios.actualizar_perfil(
        ciudadano_id=actual.id,
        direccion=solicitud.direccion,
        telefono=solicitud.telefono,
        email_personal=str(solicitud.email_personal) if solicitud.email_personal is not None else None,
        correlation_id=_correlation_id(request),
    )
    return _a_respuesta(ciudadano, cuota_bytes=cuota_bytes, usado_bytes=usado_bytes)
