import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from app.config import get_config

# Sin esto, un logger propio (p. ej. "colcarpeta.notificaciones", usado para dejar
# constancia demostrable del correo simulado de primer acceso) nunca imprime nada por
# debajo de WARNING: sin handlers configurados en ningun punto de la jerarquia, Python
# solo aplica su manejador de ultimo recurso, que filtra en WARNING. El nivel global
# se deja en WARNING (no cambia el comportamiento ya visto de uvicorn ni de librerias
# de terceros) y solo la jerarquia propia ("colcarpeta.*") baja a INFO.
logging.basicConfig(level=logging.WARNING)
logging.getLogger("colcarpeta").setLevel(logging.INFO)
from app.errors import (
    ErrorDeNegocio,
    manejar_error_de_negocio,
    manejar_error_de_validacion,
    manejar_error_no_previsto,
)
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
