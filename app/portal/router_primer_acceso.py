"""Primer acceso de un ciudadano recibido por transferencia (CU-16) -- pantallas del
portal. Ver AD-11 (docs/especificacion.md): la lógica de negocio vive en
`app.identidad.primer_acceso_servicios`, compartida con la ruta JSON
(`app.identidad.primer_acceso`).

Ninguna de las dos pantallas exige sesión -- el ciudadano todavía no puede
autenticarse -- y ninguna revela si el token o la cédula corresponden a alguien real,
igual que la API: un token que no existe, ya se usó o venció muestra el mismo mensaje,
y pedir un enlace nuevo responde siempre el mismo texto fijo.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app.errors import ErrorDeNegocio
from app.identidad.primer_acceso_servicios import MENSAJE_REENVIO, SolicitudPrimerAcceso, establecer_password, reenviar_primer_acceso
from app.portal.router import _correlation_id, _mensaje_validacion, _origen, templates

router = APIRouter(tags=["portal"], include_in_schema=False)


@router.get("/primer-acceso", response_class=HTMLResponse)
async def form_primer_acceso(request: Request) -> HTMLResponse:
    token = request.query_params.get("token", "")
    return templates.TemplateResponse(request, "primer_acceso.html", {"ciudadano": None, "token": token})


@router.post("/primer-acceso", response_class=HTMLResponse)
async def procesar_primer_acceso(request: Request, token: str = Form(...), password: str = Form(...)) -> HTMLResponse:
    try:
        solicitud = SolicitudPrimerAcceso(token=token, password=password)
    except ValidationError as exc:
        return templates.TemplateResponse(
            request, "primer_acceso.html", {"ciudadano": None, "token": token, "error": _mensaje_validacion(exc)}, status_code=422
        )

    try:
        await establecer_password(solicitud, correlation_id=_correlation_id(request))
    except ErrorDeNegocio as exc:
        return templates.TemplateResponse(
            request, "primer_acceso.html", {"ciudadano": None, "token": token, "error": exc.mensaje}, status_code=200
        )

    return RedirectResponse("/sesion?primeracceso=1", status_code=303)


@router.get("/primer-acceso/reenviar", response_class=HTMLResponse)
async def form_reenviar(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "primer_acceso_reenviar.html", {"ciudadano": None})


@router.post("/primer-acceso/reenviar", response_class=HTMLResponse)
async def procesar_reenviar(request: Request, usuario: str = Form(..., min_length=1)) -> HTMLResponse:
    try:
        await reenviar_primer_acceso(usuario=usuario, origen=_origen(request), correlation_id=_correlation_id(request))
    except ErrorDeNegocio as exc:
        return templates.TemplateResponse(
            request, "primer_acceso_reenviar.html", {"ciudadano": None, "error": exc.mensaje}, status_code=200
        )

    return templates.TemplateResponse(request, "primer_acceso_reenviar.html", {"ciudadano": None, "mensaje": MENSAJE_REENVIO})
