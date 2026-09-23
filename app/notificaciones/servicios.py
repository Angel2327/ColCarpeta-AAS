"""Capa de servicios de CU-17 (centro de notificaciones): lógica compartida entre la
ruta JSON (`app.notificaciones.router`) y la bandeja del portal (`app.portal`). Ver
AD-11 (docs/especificacion.md) para por qué el portal no llama a esa ruta por HTTP.

Devuelve instancias de `Notificacion` ya cargadas antes de que su sesión se cierre,
igual que `app.documentos.servicios` -- el portal construye su propio HTML a partir de
ellas, sin pasar por el modelo `RespuestaNotificacion` de la API.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.models import Auditoria, Notificacion

TAMANO_PAGINA_DEFECTO = 20
TAMANO_PAGINA_MAXIMO = 100


async def listar_notificaciones(
    *, ciudadano_id: int, solo_no_leidas: bool, page: int, size: int
) -> tuple[list[Notificacion], int, int, int, int]:
    """Devuelve `(items, total, no_leidas, page, size)`."""
    page = max(page, 1)
    size = max(1, min(size, TAMANO_PAGINA_MAXIMO))

    condiciones = [Notificacion.ciudadano_id == ciudadano_id]
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
                .where(Notificacion.ciudadano_id == ciudadano_id, Notificacion.leida_en.is_(None))
            )
        ).scalar_one()
        resultado = await session.execute(
            select(Notificacion)
            .where(*condiciones)
            .order_by(Notificacion.creado_en.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        items = list(resultado.scalars().all())

    return items, total, no_leidas, page, size


async def contar_no_leidas(*, ciudadano_id: int) -> int:
    """Solo el número, para la insignia de la navegación (visible en cualquier
    pantalla, refrescada por HTMX sin recargar la página)."""
    async with SessionLocal() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Notificacion)
                .where(Notificacion.ciudadano_id == ciudadano_id, Notificacion.leida_en.is_(None))
            )
        ).scalar_one()


async def marcar_leida(*, ciudadano_id: int, notificacion_id: int, correlation_id: str | None) -> Notificacion:
    async with SessionLocal() as session:
        notificacion = await session.get(Notificacion, notificacion_id)
        if notificacion is None:
            raise ErrorDeNegocio("RECURSO_NO_ENCONTRADO", "La notificación no existe.")
        if notificacion.ciudadano_id != ciudadano_id:
            raise ErrorDeNegocio("NO_AUTORIZADO", "La notificación no pertenece a tu carpeta.")

        if notificacion.leida_en is None:
            notificacion.leida_en = datetime.now(timezone.utc)
            session.add(
                Auditoria(
                    actor=str(ciudadano_id),
                    accion="notificacion.leida",
                    recurso=str(notificacion.id),
                    ciudadano_id=ciudadano_id,
                    correlation_id=correlation_id,
                    detalle={},
                )
            )
            await session.commit()
            await session.refresh(notificacion)

    return notificacion
