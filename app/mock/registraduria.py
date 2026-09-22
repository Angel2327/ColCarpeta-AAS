"""Registraduria Nacional simulada.

No forma parte del operador: cumple el papel del sistema externo que verifica la
identidad del ciudadano. Vive bajo /mock/registraduria para que quede claro en el codigo
y en los diagramas.

Comportamiento determinista segun el ultimo digito de la cedula, para poder probar los
flujos de excepcion de CU-01 sin depender del azar:
    termina en 0  -> 404, identidad no confirmada            (E2)
    termina en 9  -> demora 35 s y luego 504                 (E3)
    cualquier otro -> 200, identidad confirmada              (camino basico)
"""

import asyncio

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import get_config

router = APIRouter(prefix="/mock/registraduria/v1", tags=["registraduria (simulada)"])


class SolicitudVerificacion(BaseModel):
    cedula: int = Field(gt=0)
    nombre: str


@router.post("/verificar")
async def verificar(
    solicitud: SolicitudVerificacion,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """Simula la verificación de identidad de la Registraduría Nacional.

    Requiere una clave de API en el encabezado `X-Api-Key`. La respuesta es
    determinista según el último dígito de la cédula: termina en 0 responde 404
    (identidad no confirmada); termina en 9 demora unos segundos y responde 504; el
    resto responde 200 con la identidad confirmada.
    """
    cfg = get_config()
    if x_api_key != cfg.registraduria_api_key:
        raise HTTPException(status_code=401, detail="clave de API invalida")

    ultimo = solicitud.cedula % 10

    if ultimo == 9:
        await asyncio.sleep(35)
        raise HTTPException(status_code=504, detail="la Registraduria no respondio")

    if ultimo == 0:
        # Cuerpo plano segun el contrato documentado: HTTPException envolveria esto en
        # {"detail": ...} y el cliente de la Registraduria espera la forma exacta del spec.
        return JSONResponse(status_code=404, content={"verificado": False, "motivo": "NO_ENCONTRADO"})

    return {
        "verificado": True,
        "cedula": solicitud.cedula,
        "nombre_completo": solicitud.nombre,
        "fecha_expedicion": "2014-03-21",
        "documento_url": f"{cfg.public_base_url}/mock/registraduria/v1/documentos/{solicitud.cedula}.pdf",
        "firma": "MEUCIQD-simulada",
    }
