"""Sobre unico de error de la API propia.

Los endpoints de transferencia entre operadores NO usan este formato: responden con los
codigos y cuerpos que define el acuerdo del ecosistema.
"""

from fastapi import Request
from fastapi.responses import JSONResponse

# codigo de negocio -> codigo HTTP
CODIGOS: dict[str, int] = {
    "VALIDACION_FALLIDA": 422,
    "CREDENCIALES_INVALIDAS": 401,
    "SEGUNDO_FACTOR_REQUERIDO": 428,
    "SEGUNDO_FACTOR_INVALIDO": 401,
    "CUENTA_BLOQUEADA": 423,
    "NO_AUTORIZADO": 403,
    "RECURSO_NO_ENCONTRADO": 404,
    "CIUDADANO_YA_AFILIADO": 409,
    "CIUDADANO_YA_REGISTRADO": 409,
    "IDENTIDAD_NO_VERIFICADA": 409,
    "CUOTA_AGOTADA": 409,
    "ARCHIVO_DEMASIADO_GRANDE": 413,
    "TIPO_NO_PERMITIDO": 415,
    "DOCUMENTO_CERTIFICADO": 409,
    "ESTADO_INVALIDO": 409,
    "CENTRALIZADOR_NO_DISPONIBLE": 503,
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


async def manejar_error_de_negocio(request: Request, exc: ErrorDeNegocio) -> JSONResponse:
    return JSONResponse(
        status_code=CODIGOS[exc.codigo],
        content={
            "error": {
                "codigo": exc.codigo,
                "mensaje": exc.mensaje,
                "detalle": exc.detalle,
                "correlation_id": getattr(request.state, "correlation_id", None),
            }
        },
    )
