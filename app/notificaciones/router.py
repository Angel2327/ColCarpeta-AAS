"""CU-17: centro de notificaciones -- ruta JSON de la API.

Bandeja consultable del ciudadano con las notificaciones que `app.notificaciones.correo`
genera al "enviar" un correo (registro, primer acceso, reenvio, depósito de un
documento). No hay un mecanismo paralelo: toda notificación pasa por ahí. La lógica de
negocio vive en `app.notificaciones.servicios`, compartida con la bandeja del portal
(`app.portal`) -- ver AD-11 (docs/especificacion.md).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.identidad.dependencias import ciudadano_actual
from app.models import Ciudadano, Notificacion
from app.notificaciones import servicios

router = APIRouter(prefix="/api/v1/notificaciones", tags=["notificaciones"])


class RespuestaNotificacion(BaseModel):
    id: int
    asunto: str
    cuerpo: str
    leida: bool
    creado_en: datetime


class RespuestaListaNotificaciones(BaseModel):
    items: list[RespuestaNotificacion]
    total: int
    no_leidas: int
    page: int
    size: int


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _a_respuesta(n: Notificacion) -> RespuestaNotificacion:
    return RespuestaNotificacion(id=n.id, asunto=n.asunto, cuerpo=n.cuerpo, leida=n.leida_en is not None, creado_en=n.creado_en)


@router.get("", response_model=RespuestaListaNotificaciones)
async def listar_notificaciones(
    solo_no_leidas: bool = False,
    page: int = 1,
    size: int = servicios.TAMANO_PAGINA_DEFECTO,
    actual: Ciudadano = Depends(ciudadano_actual),
) -> RespuestaListaNotificaciones:
    """Lista las notificaciones del ciudadano autenticado, más recientes primero.

    Acepta `solo_no_leidas` para mostrar únicamente las pendientes de leer, y
    paginación (`page`, `size`; tamaño de página máximo 100). Devuelve también el
    total de no leídas, independiente del filtro aplicado.
    """
    items, total, no_leidas, page, size = await servicios.listar_notificaciones(
        ciudadano_id=actual.id, solo_no_leidas=solo_no_leidas, page=page, size=size
    )
    return RespuestaListaNotificaciones(items=[_a_respuesta(n) for n in items], total=total, no_leidas=no_leidas, page=page, size=size)


@router.post("/{notificacion_id}/leida", status_code=204, response_model=None)
async def marcar_leida(notificacion_id: int, request: Request, actual: Ciudadano = Depends(ciudadano_actual)) -> None:
    """Marca una notificación propia como leída. No tiene efecto si ya lo estaba.

    Devuelve 404 si la notificación no existe, o 403 si no pertenece al ciudadano
    autenticado.
    """
    await servicios.marcar_leida(ciudadano_id=actual.id, notificacion_id=notificacion_id, correlation_id=_correlation_id(request))
