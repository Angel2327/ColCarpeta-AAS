"""Perfil del ciudadano y segundo factor -- pantallas del portal. Ver AD-11
(docs/especificacion.md): la lógica de negocio vive en `app.identidad.perfil_servicios`,
compartida con las rutas JSON (`app.identidad.perfil`, `app.identidad.perfil_totp`).

La cédula y `email_carpeta` no aparecen como campos editables en ningún formulario de
este archivo (AD-10): el formulario de datos solo admite dirección, teléfono y correo
personal, igual que `PATCH /api/v1/perfil`.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.errors import ErrorDeNegocio
from app.identidad import perfil_servicios
from app.portal.auth import ciudadano_actual_portal
from app.portal.router import _correlation_id, templates

router = APIRouter(tags=["portal"], include_in_schema=False)


@router.get("/perfil", response_class=HTMLResponse)
async def perfil(request: Request) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/sesion", status_code=303)

    ciudadano, cuota_bytes, usado_bytes = await perfil_servicios.obtener_perfil(ciudadano_id=ciudadano.id)
    afiliacion = await perfil_servicios.estado_afiliacion(ciudadano_id=ciudadano.id)
    contexto = {"ciudadano": ciudadano, "cuota_bytes": cuota_bytes, "usado_bytes": usado_bytes, "afiliacion": afiliacion}
    if request.query_params.get("actualizado"):
        contexto["mensaje_exito"] = "Tus datos se actualizaron correctamente."
    return templates.TemplateResponse(request, "perfil.html", contexto)


@router.get("/perfil/afiliacion", response_class=HTMLResponse)
async def afiliacion_estado(request: Request) -> HTMLResponse:
    """Fragmento sondeado por HTMX desde `/perfil` mientras el ciudadano sigue
    `PENDIENTE_CENTRALIZADOR` -- mismo patrón que `_documento_estado.html`: deja de
    traer `hx-trigger` en cuanto `perfil_servicios.estado_afiliacion` devuelve `None`
    (ya `ACTIVO`) o `"fallido"` (reintentos agotados, ya no se va a resolver solo)."""
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return HTMLResponse('<div id="perfil-afiliacion"></div>')

    afiliacion = await perfil_servicios.estado_afiliacion(ciudadano_id=ciudadano.id)
    return templates.TemplateResponse(request, "_perfil_afiliacion.html", {"afiliacion": afiliacion})


@router.post("/perfil", response_class=HTMLResponse)
async def actualizar_perfil(
    request: Request,
    direccion: str = Form(..., min_length=1, max_length=500),
    telefono: str = Form(..., min_length=1, max_length=30),
    email_personal: str = Form(..., min_length=1),
) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/sesion", status_code=303)

    try:
        ciudadano_actualizado, cuota_bytes, usado_bytes = await perfil_servicios.actualizar_perfil(
            ciudadano_id=ciudadano.id,
            direccion=direccion,
            telefono=telefono,
            email_personal=email_personal,
            correlation_id=_correlation_id(request),
        )
    except ErrorDeNegocio as exc:
        ciudadano_recargado, cuota_bytes, usado_bytes = await perfil_servicios.obtener_perfil(ciudadano_id=ciudadano.id)
        afiliacion = await perfil_servicios.estado_afiliacion(ciudadano_id=ciudadano.id)
        return templates.TemplateResponse(
            request,
            "perfil.html",
            {
                "ciudadano": ciudadano_recargado,
                "cuota_bytes": cuota_bytes,
                "usado_bytes": usado_bytes,
                "afiliacion": afiliacion,
                "error": exc.mensaje,
            },
            status_code=200,
        )

    return RedirectResponse("/perfil?actualizado=1", status_code=303)


# --- segundo factor (TOTP) ----------------------------------------------------------


@router.get("/perfil/totp", response_class=HTMLResponse)
async def totp(request: Request) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/sesion", status_code=303)

    ciudadano, _, _ = await perfil_servicios.obtener_perfil(ciudadano_id=ciudadano.id)
    pendiente = await perfil_servicios.estado_totp_pendiente(ciudadano_id=ciudadano.id)
    contexto = {"ciudadano": ciudadano, "pendiente": pendiente}
    if request.query_params.get("habilitado"):
        contexto["mensaje_exito"] = "El segundo factor quedó habilitado."
    if request.query_params.get("deshabilitado"):
        contexto["mensaje_exito"] = "El segundo factor quedó deshabilitado."
    if request.query_params.get("error"):
        contexto["error"] = request.query_params["error"]
    return templates.TemplateResponse(request, "totp.html", contexto)


@router.post("/perfil/totp/iniciar")
async def iniciar_totp(request: Request) -> RedirectResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/sesion", status_code=303)

    await perfil_servicios.iniciar_enrolamiento_totp(ciudadano_id=ciudadano.id, correlation_id=_correlation_id(request))
    return RedirectResponse("/perfil/totp", status_code=303)


@router.post("/perfil/totp/confirmar", response_class=HTMLResponse)
async def confirmar_totp(request: Request, codigo: str = Form(..., min_length=1)) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/sesion", status_code=303)

    try:
        await perfil_servicios.confirmar_enrolamiento_totp(
            ciudadano_id=ciudadano.id, codigo=codigo, correlation_id=_correlation_id(request)
        )
    except ErrorDeNegocio as exc:
        return RedirectResponse(f"/perfil/totp?error={exc.mensaje}", status_code=303)

    return RedirectResponse("/perfil/totp?habilitado=1", status_code=303)


@router.post("/perfil/totp/deshabilitar", response_class=HTMLResponse)
async def deshabilitar_totp(
    request: Request, password: str = Form(..., min_length=1), codigo: str = Form(..., min_length=1)
) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/sesion", status_code=303)

    try:
        await perfil_servicios.deshabilitar_totp(
            ciudadano_id=ciudadano.id, password=password, codigo=codigo, correlation_id=_correlation_id(request)
        )
    except ErrorDeNegocio as exc:
        return RedirectResponse(f"/perfil/totp?error={exc.mensaje}", status_code=303)

    return RedirectResponse("/perfil/totp?deshabilitado=1", status_code=303)
