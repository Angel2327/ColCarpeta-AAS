"""Sobre unico de error de la API propia.

Los endpoints de transferencia entre operadores NO usan este formato: responden con los
codigos y cuerpos que define el acuerdo del ecosistema.
"""

import logging

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("colcarpeta.errores")

# codigo de negocio -> codigo HTTP
CODIGOS: dict[str, int] = {
    "VALIDACION_FALLIDA": 422,
    "CREDENCIALES_INVALIDAS": 401,
    "NO_AUTENTICADO": 401,
    "TOKEN_INVALIDO": 400,
    "SEGUNDO_FACTOR_REQUERIDO": 428,
    "SEGUNDO_FACTOR_INVALIDO": 401,
    "CUENTA_BLOQUEADA": 423,
    "NO_AUTORIZADO": 403,
    "RECURSO_NO_ENCONTRADO": 404,
    "CIUDADANO_YA_AFILIADO": 409,
    "CIUDADANO_YA_REGISTRADO": 409,
    "IDENTIDAD_NO_VERIFICADA": 409,
    "CUOTA_AGOTADA": 409,
    "TRASLADO_EN_CURSO": 409,
    "OPERADOR_NO_DISPONIBLE": 404,
    "ARCHIVO_DEMASIADO_GRANDE": 413,
    "TIPO_NO_PERMITIDO": 415,
    "DOCUMENTO_CERTIFICADO": 409,
    "ESTADO_INVALIDO": 409,
    "CENTRALIZADOR_NO_DISPONIBLE": 503,
    "REGISTRADURIA_NO_DISPONIBLE": 503,
    "LIMITE_DE_TASA": 429,
    "ERROR_INTERNO": 500,
}


class ErrorDeNegocio(Exception):
    def __init__(self, codigo: str, mensaje: str, detalle: dict | None = None) -> None:
        if codigo not in CODIGOS:
            raise ValueError(f"codigo de error desconocido: {codigo}")
        self.codigo = codigo
        self.mensaje = mensaje
        self.detalle = detalle or {}
        super().__init__(mensaje)


def _sobre(request: Request, *, codigo: str, mensaje: str, detalle: dict) -> dict:
    return {
        "error": {
            "codigo": codigo,
            "mensaje": mensaje,
            "detalle": detalle,
            "correlation_id": getattr(request.state, "correlation_id", None),
        }
    }


async def manejar_error_de_negocio(request: Request, exc: ErrorDeNegocio) -> JSONResponse:
    return JSONResponse(
        status_code=CODIGOS[exc.codigo],
        content=_sobre(request, codigo=exc.codigo, mensaje=exc.mensaje, detalle=exc.detalle),
    )


async def manejar_error_de_validacion(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=CODIGOS["VALIDACION_FALLIDA"],
        content=_sobre(
            request,
            codigo="VALIDACION_FALLIDA",
            mensaje="Uno o mas campos del cuerpo de la peticion son invalidos.",
            detalle={"errores": jsonable_encoder(exc.errors())},
        ),
    )


async def manejar_error_no_previsto(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("error no previsto atendiendo %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=CODIGOS["ERROR_INTERNO"],
        # El detalle no se expone al cliente (tabla de "Formato de errores").
        content=_sobre(request, codigo="ERROR_INTERNO", mensaje="Ocurrio un error inesperado.", detalle={}),
    )
