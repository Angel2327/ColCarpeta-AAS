"""CU-17 (centro de notificaciones) -- pantallas del portal. Ver AD-11
(docs/especificacion.md): la lógica de negocio vive en `app.notificaciones.servicios`,
compartida con la ruta JSON (`app.notificaciones.router`).

`GET /notificaciones/contador` es aparte: no es una pantalla, es el fragmento que la
insignia de "Notificaciones" en la navegación (visible en cualquier pantalla,
`base.html`) sondea cada 30 s por su cuenta vía HTMX, para que el contador de no
leídas nunca dependa de que el ciudadano recargue la página a ciegas.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.errors import ErrorDeNegocio
from app.notificaciones import servicios
from app.portal.auth import ciudadano_actual_portal
from app.portal.router import _correlation_id, templates

router = APIRouter(tags=["portal"], include_in_schema=False)


@router.get("/notificaciones/contador", response_class=HTMLResponse)
async def contador_notificaciones(request: Request) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        # El sondeo sigue corriendo en pestañas viejas tras cerrar sesion en otra --
        # sin sesion, se responde un fragmento inerte (sin `hx-trigger`) en vez de un
        # redirect, que HTMX seguiria de verdad y desordenaria la pagina.
        return HTMLResponse('<a href="/sesion" id="notificaciones-contador">Notificaciones</a>')

    no_leidas = await servicios.contar_no_leidas(ciudadano_id=ciudadano.id)
    return templates.TemplateResponse(request, "_notificaciones_contador.html", {"no_leidas": no_leidas})


@router.get("/notificaciones", response_class=HTMLResponse)
async def notificaciones(request: Request) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/sesion", status_code=303)

    solo_no_leidas = request.query_params.get("solo_no_leidas") == "1"
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1

    items, total, no_leidas, page, size = await servicios.listar_notificaciones(
        ciudadano_id=ciudadano.id, solo_no_leidas=solo_no_leidas, page=page, size=servicios.TAMANO_PAGINA_DEFECTO
    )
    return templates.TemplateResponse(
        request,
        "notificaciones.html",
        {
            "ciudadano": ciudadano,
            "items": items,
            "total": total,
            "no_leidas": no_leidas,
            "page": page,
            "size": size,
            "solo_no_leidas": solo_no_leidas,
        },
    )


@router.post("/notificaciones/{notificacion_id}/leida", response_class=HTMLResponse)
async def marcar_leida(request: Request, notificacion_id: int) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/sesion", status_code=303)

    try:
        notificacion = await servicios.marcar_leida(
            ciudadano_id=ciudadano.id, notificacion_id=notificacion_id, correlation_id=_correlation_id(request)
        )
    except ErrorDeNegocio as exc:
        if request.headers.get("hx-request") == "true":
            return HTMLResponse(f'<li class="notificacion-fila" role="alert">{exc.mensaje}</li>', status_code=200)
        return RedirectResponse(f"/notificaciones?error={exc.mensaje}", status_code=303)

    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(request, "_notificacion_fila.html", {"n": notificacion})
    return RedirectResponse("/notificaciones", status_code=303)
