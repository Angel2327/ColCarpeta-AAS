"""Consola de administracion del operador (RF32-RF37, CU-22): pantallas de solo
lectura sobre las tablas existentes, para ver que esta pasando -- en particular lo que
no se ve en ningun otro lado, como una transferencia entrante (CU-16 no persiste una
fila propia para eso, ver `app.admin.servicios.listar_transferencias`). Nunca actua: no
hay boton que borre, reintente ni edite nada. Ver la AD nueva en
docs/especificacion.md para el porque de las tres decisiones de esta consola (solo
lectura, sin documentos, credencial en variable de entorno en vez de un modelo de
usuarios).

Bajo `/admin`, fuera del esquema OpenAPI (`include_in_schema=False`) y sin ningun
enlace desde el portal del ciudadano (ver AD-12: se consideró un enlace discreto en el
pie del portal y se descartó -- anunciaría la ruta de la consola a cualquier ciudadano,
no solo a quien opera el sistema). La consola sí enlaza de vuelta al portal desde su
propia cabecera, en la otra dirección: eso no expone nada nuevo. Sesion propia
(`app.admin.auth`), separada de la del ciudadano: misma cookie HttpOnly + Secure +
SameSite, pero un JWT independiente y sin ningun `Ciudadano` detras -- una unica
credencial en `ADMIN_PASSWORD_HASH`.

Mismo patron que `app.portal.router`: cada vista repite el chequeo de sesion al
principio (sin una dependencia que oculte el redirect), para que quede claro a simple
vista que pasa sin sesion.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin import servicios
from app.admin.auth import admin_autenticado, borrar_cookie_admin, emitir_token_admin, fijar_cookie_admin
from app.config import get_config
from app.portal.estaticos import url_estatica

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Mismo estilo del portal (docs/diseno.md), reutilizado tal cual -- ninguna hoja de
# estilos propia de la consola.
templates.env.globals["estatico"] = url_estatica


def _origen(request: Request) -> str:
    return request.client.host if request.client else "desconocido"


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _requiere_sesion(request: Request) -> RedirectResponse | None:
    if not admin_autenticado(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


def _url_pagina_factory(base_path: str, filtros: dict[str, str]):
    base = {k: v for k, v in filtros.items() if v}

    def _url(pagina: int) -> str:
        parametros = dict(base)
        parametros["page"] = str(pagina)
        return f"{base_path}?" + urlencode(parametros)

    return _url


async def _marcar_pantalla_vista(request: Request, pantalla: str) -> None:
    await servicios.registrar_acceso(
        accion="admin.pantalla_vista",
        origen=_origen(request),
        correlation_id=_correlation_id(request),
        detalle={"pantalla": pantalla},
    )


# --- acceso --------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def form_login(request: Request) -> HTMLResponse:
    if admin_autenticado(request):
        return RedirectResponse("/admin/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"mostrar_nav": False})


@router.post("/login", response_class=HTMLResponse)
async def procesar_login(request: Request, password: str = Form(...)) -> HTMLResponse:
    resultado = await servicios.verificar_credenciales_admin(
        password=password, origen=_origen(request), correlation_id=_correlation_id(request)
    )
    if resultado == "bloqueado":
        cfg = get_config()
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "mostrar_nav": False,
                "error": f"Demasiados intentos fallidos. Intenta de nuevo en {cfg.bloqueo_login_minutos} minutos.",
            },
            status_code=200,
        )
    if resultado == "invalido":
        return templates.TemplateResponse(
            request, "login.html", {"mostrar_nav": False, "error": "Contraseña incorrecta."}, status_code=200
        )

    token, expires_in = emitir_token_admin()
    respuesta = RedirectResponse("/admin/", status_code=303)
    fijar_cookie_admin(respuesta, access_token=token, expires_in=expires_in)
    return respuesta


@router.post("/salir")
async def salir(request: Request) -> RedirectResponse:
    respuesta = RedirectResponse("/admin/login", status_code=303)
    borrar_cookie_admin(respuesta)
    return respuesta


# --- 1. resumen ------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def resumen(request: Request) -> HTMLResponse:
    redirect = _requiere_sesion(request)
    if redirect is not None:
        return redirect

    await _marcar_pantalla_vista(request, "resumen")
    datos = await servicios.obtener_resumen()
    return templates.TemplateResponse(request, "resumen.html", {"mostrar_nav": True, "resumen": datos})


# --- 2. ciudadanos -----------------------------------------------------------------


@router.get("/ciudadanos", response_class=HTMLResponse)
async def ciudadanos(request: Request) -> HTMLResponse:
    redirect = _requiere_sesion(request)
    if redirect is not None:
        return redirect

    cedula = request.query_params.get("cedula") or None
    nombre = request.query_params.get("nombre") or None
    origen = request.query_params.get("origen") or None
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1

    await _marcar_pantalla_vista(request, "ciudadanos")
    items, total, page = await servicios.listar_ciudadanos(cedula=cedula, nombre=nombre, origen=origen, page=page)
    return templates.TemplateResponse(
        request,
        "ciudadanos.html",
        {
            "mostrar_nav": True,
            "items": items,
            "total": total,
            "page": page,
            "size": servicios.TAMANO_PAGINA,
            "cedula": cedula or "",
            "nombre": nombre or "",
            "origen": origen or "",
            "url_pagina": _url_pagina_factory(
                "/admin/ciudadanos", {"cedula": cedula or "", "nombre": nombre or "", "origen": origen or ""}
            ),
        },
    )


# --- 3. transferencias ---------------------------------------------------------------


@router.get("/transferencias", response_class=HTMLResponse)
async def transferencias(request: Request) -> HTMLResponse:
    redirect = _requiere_sesion(request)
    if redirect is not None:
        return redirect

    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1

    await _marcar_pantalla_vista(request, "transferencias")
    items, total = await servicios.listar_transferencias(page=page)
    return templates.TemplateResponse(
        request,
        "transferencias.html",
        {
            "mostrar_nav": True,
            "items": items,
            "total": total,
            "page": page,
            "size": servicios.TAMANO_PAGINA,
            "url_pagina": _url_pagina_factory("/admin/transferencias", {}),
        },
    )


# --- 4. bandeja de salida ------------------------------------------------------------


@router.get("/bandeja-salida", response_class=HTMLResponse)
async def bandeja_salida(request: Request) -> HTMLResponse:
    redirect = _requiere_sesion(request)
    if redirect is not None:
        return redirect

    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1

    await _marcar_pantalla_vista(request, "bandeja_salida")
    items, total = await servicios.listar_outbox(page=page)
    return templates.TemplateResponse(
        request,
        "bandeja_salida.html",
        {
            "mostrar_nav": True,
            "items": items,
            "total": total,
            "page": page,
            "size": servicios.TAMANO_PAGINA,
            "url_pagina": _url_pagina_factory("/admin/bandeja-salida", {}),
        },
    )


# --- 5. auditoria ----------------------------------------------------------------


def _parse_fecha(valor: str | None) -> date | None:
    if not valor:
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        return None


@router.get("/auditoria", response_class=HTMLResponse)
async def auditoria(request: Request) -> HTMLResponse:
    redirect = _requiere_sesion(request)
    if redirect is not None:
        return redirect

    qp = request.query_params
    cedula = qp.get("cedula") or None
    accion_filtro = qp.get("accion") or None
    desde_f = _parse_fecha(qp.get("desde"))
    hasta_f = _parse_fecha(qp.get("hasta"))
    desde_dt = datetime.combine(desde_f, time.min, tzinfo=timezone.utc) if desde_f else None
    hasta_dt = datetime.combine(hasta_f, time.max, tzinfo=timezone.utc) if hasta_f else None
    try:
        page = max(1, int(qp.get("page", "1")))
    except ValueError:
        page = 1

    await _marcar_pantalla_vista(request, "auditoria")
    items, total = await servicios.listar_auditoria(
        cedula=cedula, accion=accion_filtro, desde=desde_dt, hasta=hasta_dt, page=page
    )
    filtros = {"cedula": cedula or "", "accion": accion_filtro or "", "desde": qp.get("desde") or "", "hasta": qp.get("hasta") or ""}
    return templates.TemplateResponse(
        request,
        "auditoria.html",
        {
            "mostrar_nav": True,
            "items": items,
            "total": total,
            "page": page,
            "size": servicios.TAMANO_PAGINA,
            "filtros": filtros,
            "url_pagina": _url_pagina_factory("/admin/auditoria", filtros),
        },
    )
