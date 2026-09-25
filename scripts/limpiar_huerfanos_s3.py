"""Borra del bucket los objetos que no tienen ninguna fila correspondiente en
`documento` -- residuo acumulado por las pruebas de Docker (docker-compose.test.yml),
que suben documentos reales al bucket S3 real pero usan una base de datos Postgres
local y desechable (`postgres-a`/`postgres-b`, borrada con `down -v` al terminar): la
fila desaparece, el objeto en el bucket nunca se limpia solo.

    python scripts/limpiar_huerfanos_s3.py            # solo lista, no borra nada
    python scripts/limpiar_huerfanos_s3.py --borrar    # pide confirmar y borra

Nunca toca un objeto que SÍ tenga una fila `documento.s3_key` -- por eso es seguro
correrlo aunque haya ciudadanos reales con documentos de verdad en el bucket.

Borra de a un objeto (`delete_object`), no por lotes: la primera version usaba
`delete_objects` (borrado multiple en una sola llamada) y la capa S3-compatible de
Supabase la rechazo con un `ClientError` sin codigo ni mensaje -- no soporta esa
operacion, aunque el resto de la API S3 que usa este proyecto (`put_object`,
`delete_object`, `get_object`, `generate_presigned_url`) si funciona bien. Con 295
objetos, uno por uno es perfectamente aceptable y ademas mas seguro: un objeto que
falle no bloquea a los demas, y queda registrado cual fue.

Herramienta de mantenimiento, no parte de la API. Borra contra el bucket real -- no
hay entorno de pruebas aparte.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import ClientError  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import get_config  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.documentos.almacenamiento import _cliente  # noqa: E402
from app.models import Documento  # noqa: E402

# Codigos de error de S3 que significan "ya no existe" -- no es un fallo, es el
# resultado que se buscaba. Se incluyen variantes porque ya se confirmo que esta capa
# S3-compatible no siempre devuelve los mismos codigos/mensajes que Amazon S3.
_CODIGOS_YA_BORRADO = {"NoSuchKey", "404", "NotFound"}


async def _claves_en_uso() -> set[str]:
    async with SessionLocal() as session:
        resultado = await session.execute(select(Documento.s3_key))
        return {fila[0] for fila in resultado.all()}


def _listar_bucket(bucket: str) -> list[str]:
    cliente = _cliente()
    paginador = cliente.get_paginator("list_objects_v2")
    claves = []
    for pagina in paginador.paginate(Bucket=bucket):
        for obj in pagina.get("Contents", []):
            claves.append(obj["Key"])
    return claves


async def main(borrar: bool) -> None:
    cfg = get_config()
    print("Comparando el bucket contra `documento.s3_key`...")
    en_uso = await _claves_en_uso()
    todas = _listar_bucket(cfg.s3_bucket)
    huerfanos = sorted(k for k in todas if k not in en_uso)

    print(f"\nObjetos en el bucket: {len(todas)}")
    print(f"Referenciados por algun documento: {len(en_uso)}")
    print(f"Huerfanos (sin fila de documento): {len(huerfanos)}\n")

    if not huerfanos:
        print("Nada que borrar.")
        return

    for clave in huerfanos:
        print(f"  {clave}")

    if not borrar:
        print("\nModo de solo lectura (sin --borrar): no se borro nada.")
        return

    respuesta = input(
        f"\nEsto borra {len(huerfanos)} objetos REALES del bucket, sin deshacer. "
        f"Escribe BORRAR para confirmar, cualquier otra cosa aborta: "
    ).strip()
    if respuesta != "BORRAR":
        print("Confirmacion no coincide. Abortado, no se borro nada.")
        return

    cliente = _cliente()
    total = len(huerfanos)
    borrados = 0
    ya_no_existian = 0
    fallidos: list[tuple[str, str]] = []

    print()
    for i, clave in enumerate(huerfanos, start=1):
        try:
            cliente.delete_object(Bucket=cfg.s3_bucket, Key=clave)
            borrados += 1
            print(f"  [{i}/{total}] OK       {clave}")
        except ClientError as exc:
            codigo = exc.response.get("Error", {}).get("Code", "")
            if codigo in _CODIGOS_YA_BORRADO:
                ya_no_existian += 1
                print(f"  [{i}/{total}] YA NO EXISTIA  {clave}")
            else:
                mensaje = str(exc)
                fallidos.append((clave, mensaje))
                print(f"  [{i}/{total}] FALLO    {clave}: {mensaje}")

    print(f"\nListo. Borrados: {borrados}. Ya no existian (se cuentan como resueltos): {ya_no_existian}. Fallidos: {len(fallidos)}.")
    if fallidos:
        print("\nObjetos que fallaron (podes volver a correr el script para reintentarlos):")
        for clave, mensaje in fallidos:
            print(f"  {clave}: {mensaje}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--borrar", action="store_true", help="borra los huerfanos (pide confirmacion); sin esto, solo lista")
    args = parser.parse_args()

    asyncio.run(main(args.borrar))
