"""Cliente de la Registraduria Nacional simulada.

Ver docs/especificacion.md, seccion "Registraduria simulada". Es un sistema externo para
CU-01 -- aunque en esta entrega se sirve desde la misma aplicacion bajo
`/mock/registraduria`, se llama por HTTP (via ASGI, sin salir a la red) y no con una
importacion directa del router, para conservar la frontera y para que timeouts y codigos
de estado se comporten igual que contra un servicio real.

Comportamiento determinista segun el ultimo digito de la cedula (util para probar E2/E3
de CU-01): terminada en 0 -> 404 no confirmada; terminada en 9 -> demora 35 s y 504;
cualquier otra -> 200 confirmada.

OJO: `httpx.ASGITransport` no hace E/S de socket real, asi que el `timeout=` del cliente
no aplica -- httpx solo hace cumplir sus timeouts en transportes que de verdad esperan en
una conexion de red. El limite de tiempo hay que forzarlo con `asyncio.wait_for`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from fastapi import FastAPI

from app.config import get_config

# Mas corto que los 35 s de demora simulada: una cedula terminada en 9 debe manifestarse
# como "no responde" (E3) sin obligar al ciudadano a esperar la demora completa.
LIMITE_SEGUNDOS = 10.0


class RegistraduriaNoDisponible(Exception):
    """La Registraduria no respondio: tiempo de espera agotado o error de red (E3)."""


@dataclass(frozen=True)
class ResultadoVerificacion:
    verificado: bool
    nombre_completo: str | None = None
    documento_url: str | None = None
    firma: str | None = None
    motivo: str | None = None


async def verificar_identidad(app: FastAPI, *, cedula: int, nombre: str) -> ResultadoVerificacion:
    """POST /mock/registraduria/v1/verificar."""
    cfg = get_config()
    transporte = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transporte, base_url="http://registraduria.interno") as cliente:
            r = await asyncio.wait_for(
                cliente.post(
                    "/mock/registraduria/v1/verificar",
                    json={"cedula": cedula, "nombre": nombre},
                    headers={"X-Api-Key": cfg.registraduria_api_key},
                ),
                timeout=LIMITE_SEGUNDOS,
            )
    except TimeoutError as exc:
        raise RegistraduriaNoDisponible("tiempo de espera agotado") from exc
    except httpx.HTTPError as exc:
        raise RegistraduriaNoDisponible(str(exc)) from exc

    if r.status_code == 200:
        cuerpo = r.json()
        return ResultadoVerificacion(
            verificado=True,
            nombre_completo=cuerpo.get("nombre_completo"),
            documento_url=cuerpo.get("documento_url"),
            firma=cuerpo.get("firma"),
        )
    if r.status_code == 404:
        cuerpo = r.json()
        return ResultadoVerificacion(verificado=False, motivo=cuerpo.get("motivo"))
    raise RegistraduriaNoDisponible(f"respuesta inesperada de la Registraduria: {r.status_code}")
