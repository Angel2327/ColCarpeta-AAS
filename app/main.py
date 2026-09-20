import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from app.config import get_config
from app.errors import (
    ErrorDeNegocio,
    manejar_error_de_negocio,
    manejar_error_de_validacion,
    manejar_error_no_previsto,
)
from app.documentos.router import router as documentos_router
from app.identidad.perfil_totp import router as perfil_totp_router
from app.identidad.registro import router as registro_router
from app.identidad.sesion import router as sesion_router
from app.interoperabilidad.outbox import ejecutar_bandeja_de_salida
from app.mock.registraduria import router as registraduria_router


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
    cfg = get_config()
    return {
        "status": "ok",
        "operador": cfg.operator_name,
        "operator_id": cfg.operator_id,
        "version": app.version,
    }


app.include_router(registraduria_router)
app.include_router(registro_router)
app.include_router(sesion_router)
app.include_router(perfil_totp_router)
app.include_router(documentos_router)
