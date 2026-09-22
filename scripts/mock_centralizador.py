"""Centralizador falso, solo para docker-compose.test.yml: permite probar CU-03
(envio) y CU-16 (recepcion) de punta a punta entre dos instancias propias, sin tocar
el MinTIC real ni el directorio compartido por los 72 equipos del curso.

Implementa el mismo contrato HTTP que docs/especificacion.md documenta para el
centralizador real -- validateCitizen, registerCitizen, unregisterCitizen,
getOperators, registerTransferEndPoint -- con estado en memoria de proceso.

NO es una reimplementacion fiel del servicio real. En particular, no se verifico
contra el servicio real que responde `unregisterCitizen` para una cedula que ya no
esta afiliada al operador que la pide (CLAUDE.md prohibe probar eso con cedulas
inventadas). Aqui se trata como exito idempotente (200) a proposito, para poder
probar reintentos de CU-03 sin adivinar el comportamiento real.

Uso: uvicorn scripts.mock_centralizador:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

app = FastAPI(title="Centralizador falso (solo pruebas, no usar contra el MinTIC real)")

# cedula -> {"operatorId", "operatorName", "name", "address", "email"}
REGISTRO: dict[int, dict] = {}

# _id -> {"operatorName", "transferAPIURL"}. Se completa con MOCK_DIRECTORIO (JSON) --
# normalmente las dos instancias del docker-compose de prueba -- y siempre incluye una
# entrada sin transferAPIURL, para ejercitar la tolerancia de "Directorio de
# operadores" (CLAUDE.md, trampa 5: datos sucios) tambien en este mock.
_DIRECTORIO_BASE = {"sin-url": {"operatorName": "Operador Sin URL (prueba)", "transferAPIURL": ""}}
DIRECTORIO: dict[str, dict] = {**_DIRECTORIO_BASE, **json.loads(os.environ.get("MOCK_DIRECTORIO", "{}"))}


@app.get("/apis/validateCitizen/{cedula}")
async def validate_citizen(cedula: int):
    registro = REGISTRO.get(cedula)
    if registro is None:
        return PlainTextResponse(status_code=204, content="")
    return PlainTextResponse(
        status_code=200,
        content=f"El ciudadano con id: {cedula} se encuentra registrado en el operador: {registro['operatorName']} ",
    )


@app.post("/apis/registerCitizen")
async def register_citizen(request: Request):
    cuerpo = await request.json()
    cedula = int(cuerpo["id"])
    if cedula in REGISTRO:
        return PlainTextResponse(status_code=501, content="citizen already registered")
    REGISTRO[cedula] = {
        "operatorId": cuerpo["operatorId"],
        "operatorName": cuerpo["operatorName"],
        "name": cuerpo["name"],
        "address": cuerpo["address"],
        "email": cuerpo["email"],
    }
    return PlainTextResponse(status_code=201, content="created")


@app.delete("/apis/unregisterCitizen")
async def unregister_citizen(request: Request):
    cuerpo = await request.json()
    cedula = int(cuerpo["id"])
    registro = REGISTRO.get(cedula)
    if registro is not None and registro.get("operatorId") != cuerpo.get("operatorId"):
        return PlainTextResponse(status_code=400, content="not your citizen")
    REGISTRO.pop(cedula, None)
    return PlainTextResponse(status_code=200, content="deleted")


@app.get("/apis/getOperators")
async def get_operators():
    return [
        {"_id": _id, "operatorName": datos["operatorName"], "transferAPIURL": datos["transferAPIURL"]}
        for _id, datos in DIRECTORIO.items()
    ]


@app.put("/apis/registerTransferEndPoint")
async def register_transfer_endpoint(request: Request):
    return JSONResponse(status_code=200, content={"ok": True})


@app.get("/_estado")
async def estado():
    """Fuera del contrato del MinTIC -- solo para inspeccionar el mock durante la
    prueba (scripts/probar_envio_transferencia.py y depuracion manual)."""
    return {"registro": REGISTRO, "directorio": DIRECTORIO}
