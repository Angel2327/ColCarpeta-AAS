import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from app.config import get_config
from app.portal.estaticos import cache_control_para, manifest_versionado

# Sin esto, un logger propio (p. ej. "colcarpeta.notificaciones", usado para dejar
# constancia demostrable del correo simulado de primer acceso) nunca imprime nada por
# debajo de WARNING: sin handlers configurados en ningun punto de la jerarquia, Python
# solo aplica su manejador de ultimo recurso, que filtra en WARNING. El nivel global
# se deja en WARNING (no cambia el comportamiento ya visto de uvicorn ni de librerias
# de terceros) y solo la jerarquia propia ("colcarpeta.*") baja a INFO.
logging.basicConfig(level=logging.WARNING)
logging.getLogger("colcarpeta").setLevel(logging.INFO)
# CU-09 deliberadamente valida firmas sin ninguna raiz de confianza (ver
# app.documentos.firma): pyHanko registra eso como WARNING en cada validacion
# ("no se pudo construir una ruta de validacion"), pero para nosotros no es una
# anomalia, es el estado permanente y esperado del sistema. Sin esto, cada documento
# firmado que se carga o se deposita llenaria los logs con un warning que no dice nada
# nuevo y ahogaria los que si importan.
logging.getLogger("pyhanko").setLevel(logging.ERROR)
from app.errors import (
    ErrorDeNegocio,
    manejar_error_de_negocio,
    manejar_error_de_validacion,
    manejar_error_no_previsto,
)
from app.admin.router import router as admin_router
from app.documentos.entidades import router as entidades_router
from app.documentos.router import router as documentos_router
from app.identidad.perfil import router as perfil_router
from app.identidad.perfil_totp import router as perfil_totp_router
from app.identidad.primer_acceso import router as primer_acceso_router
from app.identidad.registro import router as registro_router
from app.identidad.sesion import router as sesion_router
from app.interoperabilidad.outbox import ejecutar_bandeja_de_salida
from app.interoperabilidad.transferencias import router as transferencias_router
from app.interoperabilidad.transferencias import router_propio as traslado_router
from app.mock.registraduria import router as registraduria_router
from app.notificaciones.router import router as notificaciones_router
from app.portal.router import router as portal_router
from app.portal.router import router_legado as portal_router_legado
from app.portal.router_notificaciones import router as portal_notificaciones_router
from app.portal.router_perfil import router as portal_perfil_router
from app.portal.router_primer_acceso import router as portal_primer_acceso_router
from app.portal.router_traslado import router as portal_traslado_router


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    tarea_outbox = asyncio.create_task(ejecutar_bandeja_de_salida())
    try:
        yield
    finally:
        tarea_outbox.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarea_outbox


app = FastAPI(
    title="ColCarpeta — Operador de Carpeta Ciudadana",
    description=(
        "API de ColCarpeta, un operador de Carpeta Ciudadana: permite a un ciudadano "
        "registrarse, iniciar sesion, cargar y consultar sus documentos, solicitar la "
        "autenticacion de un documento ante el sistema nacional GovCarpeta, y trasladar "
        "su carpeta a otro operador. Las rutas bajo `/api` (sin version) implementan el "
        "acuerdo de interoperabilidad entre operadores y no llevan el sobre de error de "
        "las demas rutas."
    ),
    version="0.1.0",
    lifespan=ciclo_de_vida,
)
app.add_exception_handler(ErrorDeNegocio, manejar_error_de_negocio)
app.add_exception_handler(RequestValidationError, manejar_error_de_validacion)
app.add_exception_handler(Exception, manejar_error_no_previsto)


@app.middleware("http")
async def correlacion(request: Request, call_next):
    request.state.correlation_id = request.headers.get("X-Correlation-Id") or uuid.uuid4().hex[:12]
    respuesta = await call_next(request)
    respuesta.headers["X-Correlation-Id"] = request.state.correlation_id
    return respuesta


@app.get("/health", tags=["operacion"])
async def health():
    """Verifica que el servicio esté disponible.

    Devuelve el nombre e identificador del operador ante el MinTIC y la versión de la
    API. No requiere autenticación.
    """
    cfg = get_config()
    return {
        "status": "ok",
        "operador": cfg.operator_name,
        "operator_id": cfg.operator_id,
        "version": app.version,
    }


app.include_router(registraduria_router)
app.include_router(registro_router)
app.include_router(primer_acceso_router)
app.include_router(sesion_router)
app.include_router(perfil_router)
app.include_router(perfil_totp_router)
app.include_router(documentos_router)
app.include_router(entidades_router)
app.include_router(notificaciones_router)
app.include_router(transferencias_router)
app.include_router(traslado_router)
# El portal vive en la raiz del dominio (AD-11): / lleva a la carpeta o a iniciar
# sesion, sin prefijo -- pero no toca ninguna ruta de /api/, /health, /mock/ ni las
# que expone FastAPI (/docs, /redoc, /openapi.json). portal_router_legado redirige de
# forma permanente cada ruta vieja bajo /portal/... hacia su equivalente nueva.
app.include_router(portal_router)
app.include_router(portal_notificaciones_router)
app.include_router(portal_perfil_router)
app.include_router(portal_traslado_router)
app.include_router(portal_primer_acceso_router)
app.include_router(portal_router_legado)
# Consola de administracion (RF32-RF37, CU-22): bajo /admin, fuera del esquema OpenAPI
# (declarado include_in_schema=False en el propio router) y sin ningun enlace desde el
# portal (ver AD-12: un enlace, aunque discreto, anunciaria la ruta a cualquier
# ciudadano). La consola si enlaza de vuelta al portal desde su cabecera.
app.include_router(admin_router)


@app.get("/static/site.webmanifest", include_in_schema=False)
async def manifest(request: Request):
    """Registrada antes del `mount` de abajo para tomar precedencia sobre el archivo
    real del mismo nombre: `site.webmanifest` declara sus propios iconos con una URL
    fija, y esta version reescribe esas URL para que tambien lleven la version por
    contenido (ver app.portal.estaticos)."""
    respuesta = Response(content=manifest_versionado(), media_type="application/manifest+json")
    respuesta.headers["Cache-Control"] = cache_control_para(request.scope.get("query_string", b""))
    return respuesta


class EstaticosVersionados(StaticFiles):
    """Cache agresiva de verdad, pero solo para una URL versionada
    (`app.portal.estaticos.url_estatica`): esa nunca cambia de contenido, un archivo
    que cambia siempre estrena URL. Una peticion SIN el parametro de version -- la
    redireccion historica `/portal/static/...` sigue apuntando a `/static/...` sin
    version, a proposito, para no romper un enlace guardado -- no recibe la misma
    cabecera: esa URL si puede cambiar de contenido en el proximo despliegue, y
    guardarla un año habria sido peor que el problema original que esto vino a
    resolver, porque ya ni un despliegue nuevo la corrige (encontrado el 2026-09-24,
    antes de que este ajuste llegara a producción)."""

    async def get_response(self, path: str, scope) -> Response:
        respuesta = await super().get_response(path, scope)
        respuesta.headers["Cache-Control"] = cache_control_para(scope.get("query_string", b""))
        return respuesta


# AD-11: el portal sirve sus propios estaticos (HTMX vendorizado, hoja de estilos) desde
# el mismo proceso -- ninguna dependencia de red en tiempo de ejecucion.
app.mount(
    "/static", EstaticosVersionados(directory=str(Path(__file__).parent / "portal" / "static")), name="portal-static"
)
