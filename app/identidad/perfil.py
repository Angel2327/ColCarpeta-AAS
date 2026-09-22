"""Datos del ciudadano y estado de su carpeta.

Ver docs/especificacion.md, "Contrato de la API propia" > "Ciudadano y sesion":
`GET /api/v1/perfil` ya estaba documentado ahi pero nunca se habia implementado.
`PATCH` es una extension propia para los datos de contacto que la especificacion no fija
como inmutables (a diferencia de la cedula y `email_carpeta`, ver AD-10).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db import SessionLocal
from app.identidad.dependencias import ciudadano_actual
from app.models import Auditoria, Ciudadano, Documento, EstadoCiudadano, EstadoDocumento, EstadoTotp

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


async def _usado_bytes(session: AsyncSession, ciudadano_id: int) -> int:
    # Mismo calculo que la carga de documentos (app.documentos.router): solo cuenta lo
    # temporal y ACTIVO -- los certificados no consumen cuota, y lo reemplazado (CU-10)
    # o eliminado (CU-08) ya no forma parte de la carpeta vigente del ciudadano.
    r = await session.execute(
        select(func.coalesce(func.sum(Documento.tamano_bytes), 0))
        .select_from(Documento)
        .where(
            Documento.ciudadano_id == ciudadano_id,
            Documento.certificado.is_(False),
            Documento.estado == EstadoDocumento.ACTIVO,
        )
    )
    return int(r.scalar_one())


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
    cfg = get_config()
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, actual.id)
        assert ciudadano is not None
        usado = await _usado_bytes(session, ciudadano.id)
        return _a_respuesta(ciudadano, cuota_bytes=cfg.cuota_ciudadano_bytes, usado_bytes=usado)


@router.patch("", response_model=RespuestaPerfil)
async def actualizar_perfil(
    solicitud: SolicitudActualizarPerfil, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> RespuestaPerfil:
    """Actualiza los datos de contacto del ciudadano autenticado.

    Admite dirección, teléfono y correo personal; los campos que no se incluyan quedan
    sin modificar. La cédula y la dirección de carpeta (`email_carpeta`) son
    permanentes y no se pueden cambiar por esta vía.
    """
    cfg = get_config()
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, actual.id)
        assert ciudadano is not None

        cambios: dict[str, str] = {}
        if solicitud.direccion is not None:
            ciudadano.direccion = solicitud.direccion
            cambios["direccion"] = solicitud.direccion
        if solicitud.telefono is not None:
            ciudadano.telefono = solicitud.telefono
            cambios["telefono"] = solicitud.telefono
        if solicitud.email_personal is not None:
            ciudadano.email_personal = str(solicitud.email_personal)
            cambios["email_personal"] = str(solicitud.email_personal)

        if cambios:
            session.add(
                Auditoria(
                    actor=str(ciudadano.id),
                    accion="perfil.actualizado",
                    recurso=str(ciudadano.id),
                    ciudadano_id=ciudadano.id,
                    correlation_id=_correlation_id(request),
                    detalle=cambios,
                )
            )
            await session.commit()

        usado = await _usado_bytes(session, ciudadano.id)
        return _a_respuesta(ciudadano, cuota_bytes=cfg.cuota_ciudadano_bytes, usado_bytes=usado)
