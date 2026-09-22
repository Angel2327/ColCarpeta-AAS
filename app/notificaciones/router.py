"""CU-17: centro de notificaciones.

Bandeja consultable del ciudadano con las notificaciones que `app.notificaciones.correo`
genera al "enviar" un correo (registro, primer acceso, reenvio). No hay un mecanismo
paralelo: toda notificacion pasa por ahi.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.identidad.dependencias import ciudadano_actual
from app.models import Auditoria, Ciudadano, Notificacion

router = APIRouter(prefix="/api/v1/notificaciones", tags=["notificaciones"])

TAMANO_PAGINA_DEFECTO = 20
TAMANO_PAGINA_MAXIMO = 100


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
    size: int = TAMANO_PAGINA_DEFECTO,
    actual: Ciudadano = Depends(ciudadano_actual),
) -> RespuestaListaNotificaciones:
    """Lista las notificaciones del ciudadano autenticado, más recientes primero.

    Acepta `solo_no_leidas` para mostrar únicamente las pendientes de leer, y
    paginación (`page`, `size`; tamaño de página máximo 100). Devuelve también el
    total de no leídas, independiente del filtro aplicado.
    """
    page = max(page, 1)
    size = max(1, min(size, TAMANO_PAGINA_MAXIMO))

    condiciones = [Notificacion.ciudadano_id == actual.id]
    if solo_no_leidas:
        condiciones.append(Notificacion.leida_en.is_(None))

    async with SessionLocal() as session:
        total = (
            await session.execute(select(func.count()).select_from(Notificacion).where(*condiciones))
        ).scalar_one()
        no_leidas = (
            await session.execute(
                select(func.count())
                .select_from(Notificacion)
                .where(Notificacion.ciudadano_id == actual.id, Notificacion.leida_en.is_(None))
            )
        ).scalar_one()
        resultado = await session.execute(
            select(Notificacion)
            .where(*condiciones)
            .order_by(Notificacion.creado_en.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        items = resultado.scalars().all()

    return RespuestaListaNotificaciones(
        items=[_a_respuesta(n) for n in items], total=total, no_leidas=no_leidas, page=page, size=size
    )


@router.post("/{notificacion_id}/leida", status_code=204, response_model=None)
async def marcar_leida(notificacion_id: int, request: Request, actual: Ciudadano = Depends(ciudadano_actual)) -> None:
    """Marca una notificación propia como leída. No tiene efecto si ya lo estaba.

    Devuelve 404 si la notificación no existe, o 403 si no pertenece al ciudadano
    autenticado.
    """
    async with SessionLocal() as session:
        notificacion = await session.get(Notificacion, notificacion_id)
        if notificacion is None:
            raise ErrorDeNegocio("RECURSO_NO_ENCONTRADO", "La notificacion no existe.")
        if notificacion.ciudadano_id != actual.id:
            raise ErrorDeNegocio("NO_AUTORIZADO", "La notificacion no pertenece a tu carpeta.")

        if notificacion.leida_en is None:
            notificacion.leida_en = datetime.now(timezone.utc)
            session.add(
                Auditoria(
                    actor=str(actual.id),
                    accion="notificacion.leida",
                    recurso=str(notificacion.id),
                    ciudadano_id=actual.id,
                    correlation_id=_correlation_id(request),
                    detalle={},
                )
            )
            await session.commit()
