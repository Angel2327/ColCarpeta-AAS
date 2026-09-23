"""Portal del ciudadano: pantallas HTML servidas por la misma aplicación FastAPI
(AD-11, docs/especificacion.md). Cada ruta aquí es una vista sobre la misma capa de
servicios que usan las rutas JSON (`app.identidad.servicios`, `app.documentos.servicios`)
-- nunca llama a esas rutas por HTTP, ni duplica su lógica.

La sesión viaja en una cookie (`app.portal.auth`), no en un token que JavaScript pueda
leer. Toda ruta que exige sesión repite el mismo par de líneas al principio (leer la
cookie, redirigir a `/portal/sesion` si no hay ciudadano) en vez de una dependencia que
oculte el redirect -- es deliberado, para que cada vista deje claro a simple vista qué
pasa si no hay sesión.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.documentos import servicios as documentos_servicios
from app.errors import ErrorDeNegocio
from app.identidad.servicios import RespuestaSesion, SolicitudRegistro, SolicitudSesion, cerrar_sesion, iniciar_sesion, registrar_ciudadano
from app.portal.auth import borrar_cookie_sesion, ciudadano_actual_portal, fijar_cookie_sesion

router = APIRouter(prefix="/portal", tags=["portal"], include_in_schema=False)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _origen(request: Request) -> str:
    return request.client.host if request.client else "desconocido"


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _mensaje_validacion(exc: ValidationError) -> str:
    """Convierte los errores de Pydantic (formato de campo/tipo) en una frase en
    lenguaje llano, para no mostrarle al ciudadano la forma cruda del error."""
    partes = []
    for error in exc.errors():
        mensaje = str(error["msg"])
        if mensaje.startswith("Value error, "):
            mensaje = mensaje[len("Value error, ") :]
        partes.append(mensaje)
    return " ".join(partes) or "Revisa los datos del formulario."


def _parse_fecha(valor: str | None) -> date | None:
    if not valor:
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        return None


# --- raiz --------------------------------------------------------------------------


@router.get("")
async def raiz(request: Request) -> RedirectResponse:
    ciudadano = await ciudadano_actual_portal(request)
    destino = "/portal/carpeta" if ciudadano is not None else "/portal/sesion"
    return RedirectResponse(destino, status_code=303)


# --- CU-01/CU-02: registro e inicio de sesion ---------------------------------------


@router.get("/registro", response_class=HTMLResponse)
async def form_registro(request: Request) -> HTMLResponse:
    if await ciudadano_actual_portal(request) is not None:
        return RedirectResponse("/portal/carpeta", status_code=303)
    return templates.TemplateResponse(request, "registro.html", {"ciudadano": None})


@router.post("/registro", response_class=HTMLResponse)
async def procesar_registro(
    request: Request,
    cedula: str = Form(...),
    nombre: str = Form(...),
    direccion: str = Form(...),
    telefono: str = Form(...),
    email_personal: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    valores = {"cedula": cedula, "nombre": nombre, "direccion": direccion, "telefono": telefono, "email_personal": email_personal}
    try:
        solicitud = SolicitudRegistro(
            cedula=cedula, nombre=nombre, direccion=direccion, telefono=telefono, email_personal=email_personal, password=password
        )
    except ValidationError as exc:
        return templates.TemplateResponse(
            request, "registro.html", {"ciudadano": None, "error": _mensaje_validacion(exc), "valores": valores}, status_code=422
        )

    try:
        await registrar_ciudadano(solicitud, request.app, correlation_id=_correlation_id(request))
    except ErrorDeNegocio as exc:
        return templates.TemplateResponse(
            request, "registro.html", {"ciudadano": None, "error": exc.mensaje, "valores": valores}, status_code=200
        )

    return RedirectResponse("/portal/sesion?registrado=1", status_code=303)


@router.get("/sesion", response_class=HTMLResponse)
async def form_sesion(request: Request) -> HTMLResponse:
    if await ciudadano_actual_portal(request) is not None:
        return RedirectResponse("/portal/carpeta", status_code=303)
    contexto = {"ciudadano": None}
    if request.query_params.get("registrado"):
        contexto["mensaje_exito"] = "Tu carpeta se creo correctamente. Ya puedes iniciar sesion."
    return templates.TemplateResponse(request, "sesion.html", contexto)


@router.post("/sesion", response_class=HTMLResponse)
async def procesar_sesion(
    request: Request,
    usuario: str = Form(...),
    password: str = Form(...),
    codigo_totp: str | None = Form(None),
) -> HTMLResponse:
    solicitud = SolicitudSesion(usuario=usuario, password=password, codigo_totp=codigo_totp or None)

    try:
        resultado: RespuestaSesion = await iniciar_sesion(solicitud, origen=_origen(request), correlation_id=_correlation_id(request))
    except ErrorDeNegocio as exc:
        if exc.codigo == "SEGUNDO_FACTOR_REQUERIDO":
            return templates.TemplateResponse(request, "sesion.html", {"ciudadano": None, "usuario": usuario, "mostrar_totp": True})
        return templates.TemplateResponse(
            request,
            "sesion.html",
            {"ciudadano": None, "usuario": usuario, "error": exc.mensaje, "mostrar_totp": codigo_totp is not None},
            status_code=200,
        )

    respuesta = RedirectResponse("/portal/carpeta", status_code=303)
    fijar_cookie_sesion(respuesta, access_token=resultado.access_token, expires_in=resultado.expires_in)
    return respuesta


@router.post("/salir")
async def salir(request: Request) -> RedirectResponse:
    ciudadano = await ciudadano_actual_portal(request)
    respuesta = RedirectResponse("/portal/sesion", status_code=303)
    if ciudadano is not None:
        await cerrar_sesion(ciudadano_id=ciudadano.id, origen=_origen(request), correlation_id=_correlation_id(request))
    borrar_cookie_sesion(respuesta)
    return respuesta


# --- CU-06/07/05/08: la carpeta -----------------------------------------------------


def _filtros_desde_query(request: Request) -> dict:
    qp = request.query_params
    certificado_raw = qp.get("certificado")
    certificado = {"true": True, "false": False}.get(certificado_raw)
    return {
        "q": qp.get("q") or None,
        "tipo": qp.get("tipo") or None,
        "entidad": qp.get("entidad") or None,
        "certificado": certificado,
        "desde": _parse_fecha(qp.get("desde")),
        "hasta": _parse_fecha(qp.get("hasta")),
    }


def _url_pagina_factory(filtros: dict, size: int):
    base = {k: v for k, v in filtros.items() if v is not None and v != ""}
    if "certificado" in base:
        base["certificado"] = "true" if base["certificado"] else "false"
    if "desde" in base and base["desde"] is not None:
        base["desde"] = base["desde"].isoformat()
    if "hasta" in base and base["hasta"] is not None:
        base["hasta"] = base["hasta"].isoformat()

    def _url(pagina: int) -> str:
        parametros = dict(base)
        parametros["page"] = str(pagina)
        parametros["size"] = str(size)
        return "/portal/carpeta?" + urlencode(parametros)

    return _url


@router.get("/carpeta", response_class=HTMLResponse)
async def carpeta(request: Request) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/portal/sesion", status_code=303)

    filtros = _filtros_desde_query(request)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    try:
        size = int(request.query_params.get("size", str(documentos_servicios.TAMANO_PAGINA_DEFECTO)))
    except ValueError:
        size = documentos_servicios.TAMANO_PAGINA_DEFECTO

    items, total, page, size = await documentos_servicios.listar_documentos(
        ciudadano_id=ciudadano.id,
        tipo=filtros["tipo"],
        entidad=filtros["entidad"],
        desde=filtros["desde"],
        hasta=filtros["hasta"],
        certificado=filtros["certificado"],
        estado_autenticacion=None,
        q=filtros["q"],
        page=page,
        size=size,
    )

    hay_filtros = any(filtros.values())
    contexto = {
        "ciudadano": ciudadano,
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "filtros": filtros,
        "hay_filtros": hay_filtros,
        "url_pagina": _url_pagina_factory(filtros, size),
    }
    if request.query_params.get("subido"):
        contexto["mensaje_exito"] = "El documento se subio correctamente."
    elif request.query_params.get("eliminado"):
        contexto["mensaje_exito"] = "El documento se elimino de tu carpeta."
    if request.query_params.get("error"):
        contexto["error"] = request.query_params["error"]
    return templates.TemplateResponse(request, "carpeta.html", contexto)


@router.post("/carpeta/documentos", response_class=HTMLResponse)
async def subir_documento(
    request: Request,
    archivo: UploadFile,
    titulo: str = Form(..., min_length=1, max_length=255),
    tipo: str = Form(..., min_length=1, max_length=100),
    entidad_emisora: str | None = Form(None, max_length=255),
    fecha_emision: str | None = Form(None),
) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/portal/sesion", status_code=303)

    contenido = await archivo.read()
    try:
        await documentos_servicios.cargar_documento(
            ciudadano_id=ciudadano.id,
            contenido=contenido,
            titulo=titulo,
            tipo=tipo,
            entidad_emisora=entidad_emisora or None,
            fecha_emision=_parse_fecha(fecha_emision),
            sustituye_a=None,
            correlation_id=_correlation_id(request),
        )
    except ErrorDeNegocio as exc:
        filtros = _filtros_desde_query(request)
        items, total, page, size = await documentos_servicios.listar_documentos(
            ciudadano_id=ciudadano.id,
            tipo=None,
            entidad=None,
            desde=None,
            hasta=None,
            certificado=None,
            estado_autenticacion=None,
            q=None,
            page=1,
            size=documentos_servicios.TAMANO_PAGINA_DEFECTO,
        )
        return templates.TemplateResponse(
            request,
            "carpeta.html",
            {
                "ciudadano": ciudadano,
                "items": items,
                "total": total,
                "page": page,
                "size": size,
                "filtros": filtros,
                "hay_filtros": False,
                "url_pagina": _url_pagina_factory(filtros, size),
                "error_carga": exc.mensaje,
                "valores_carga": {"titulo": titulo, "tipo": tipo, "entidad_emisora": entidad_emisora, "fecha_emision": fecha_emision},
            },
            status_code=200,
        )

    return RedirectResponse("/portal/carpeta?subido=1", status_code=303)


# --- CU-06/CU-08/CU-11: detalle de un documento --------------------------------------


@router.get("/documentos/{documento_id}", response_class=HTMLResponse)
async def detalle_documento(request: Request, documento_id: uuid.UUID) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/portal/sesion", status_code=303)

    try:
        documento = await documentos_servicios.obtener_documento(ciudadano_id=ciudadano.id, documento_id=documento_id)
    except ErrorDeNegocio as exc:
        return RedirectResponse(f"/portal/carpeta?error={exc.mensaje}", status_code=303)

    return templates.TemplateResponse(request, "documento.html", {"ciudadano": ciudadano, "doc": documento})


@router.get("/documentos/{documento_id}/descarga")
async def descargar_documento(request: Request, documento_id: uuid.UUID) -> RedirectResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/portal/sesion", status_code=303)

    try:
        url, _ = await documentos_servicios.generar_descarga(
            ciudadano_id=ciudadano.id, documento_id=documento_id, correlation_id=_correlation_id(request)
        )
    except ErrorDeNegocio as exc:
        return RedirectResponse(f"/portal/carpeta?error={exc.mensaje}", status_code=303)

    return RedirectResponse(url, status_code=303)


@router.post("/documentos/{documento_id}/eliminar", response_class=HTMLResponse)
async def eliminar_documento(request: Request, documento_id: uuid.UUID) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/portal/sesion", status_code=303)

    es_htmx = request.headers.get("hx-request") == "true"
    try:
        await documentos_servicios.eliminar_documento(
            ciudadano_id=ciudadano.id, documento_id=documento_id, correlation_id=_correlation_id(request)
        )
    except ErrorDeNegocio as exc:
        if es_htmx:
            return HTMLResponse(f'<li class="documento-fila" role="alert">{exc.mensaje}</li>', status_code=200)
        return RedirectResponse(f"/portal/carpeta?error={exc.mensaje}", status_code=303)

    if es_htmx:
        return templates.TemplateResponse(request, "_documento_eliminado.html", {"doc_id": documento_id})
    return RedirectResponse("/portal/carpeta?eliminado=1", status_code=303)


@router.post("/documentos/{documento_id}/autenticacion")
async def solicitar_autenticacion_documento(request: Request, documento_id: uuid.UUID) -> RedirectResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/portal/sesion", status_code=303)

    try:
        await documentos_servicios.solicitar_autenticacion(
            ciudadano_id=ciudadano.id, documento_id=documento_id, correlation_id=_correlation_id(request)
        )
    except ErrorDeNegocio as exc:
        return RedirectResponse(f"/portal/carpeta?error={exc.mensaje}", status_code=303)

    return RedirectResponse(f"/portal/documentos/{documento_id}", status_code=303)
