"""Borra toda huella local y en el centralizador de un ciudadano de prueba.

    python scripts/limpiar_prueba.py <cedula>

Deja el sistema como si ese ciudadano nunca hubiera existido:
  - borra los objetos de sus documentos en el bucket,
  - borra sus filas de `documento`, `transferencia` y las entradas de `outbox` asociadas
    (localizadas por el campo `cedula` dentro del payload, ya que `outbox` no tiene
    columna `ciudadano_id`: ver docs/especificacion.md, "Modelo de datos"),
  - lo desliga del centralizador con `unregisterCitizen`,
  - y por ultimo borra la fila de `ciudadano`.

Las filas de `auditoria` NO se tocan: son de solo insercion (CLAUDE.md) y la
foreign key a `ciudadano` tiene `ON DELETE SET NULL`, asi que al borrar el
ciudadano quedan con `ciudadano_id = NULL` en vez de desaparecer o bloquear el borrado.

Herramienta de desarrollo para limpiar datos de prueba, no parte de la API.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Permite `python scripts/limpiar_prueba.py <cedula>` sin depender de PYTHONPATH: al
# ejecutar un script directamente, Python solo agrega su propio directorio a sys.path,
# no la raiz del repo (el mismo problema que scripts/probar_govcarpeta.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.documentos.almacenamiento import FalloAlmacenamiento, eliminar_objeto  # noqa: E402
from app.interoperabilidad.govcarpeta import GovCarpeta  # noqa: E402
from app.models import Ciudadano, Documento, Outbox, Transferencia  # noqa: E402


async def _imprimir_plan(
    cedula: int,
    ciudadano: Ciudadano | None,
    documentos: list[Documento],
    transferencias: list[Transferencia],
    entradas_outbox: list[Outbox],
) -> None:
    print(f"Limpieza de la cedula {cedula}. Esto es lo que se va a borrar:\n")
    print(f"  ciudadano: {'existe, estado ' + ciudadano.estado.value if ciudadano else 'no existe localmente'}")

    print(f"  documento ({len(documentos)}):")
    for d in documentos:
        print(f"    - {d.id}  {d.titulo!r}  s3_key={d.s3_key}")

    print(f"  transferencia ({len(transferencias)}):")
    for t in transferencias:
        print(f"    - {t.id}  estado={t.estado.value}")

    print(f"  outbox ({len(entradas_outbox)}):")
    for o in entradas_outbox:
        print(f"    - {o.id}  {o.operacion}  estado={o.estado.value}")

    print("\n  se invocara unregisterCitizen contra el centralizador del MinTIC")
    print("  auditoria NO se borra: sus filas quedan con ciudadano_id = NULL (ON DELETE SET NULL)\n")


async def main(cedula: int) -> None:
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, cedula)
        documentos = (
            await session.execute(select(Documento).where(Documento.ciudadano_id == cedula))
        ).scalars().all()
        transferencias = (
            await session.execute(select(Transferencia).where(Transferencia.ciudadano_id == cedula))
        ).scalars().all()
        entradas_outbox = (
            await session.execute(select(Outbox).where(Outbox.payload["cedula"].astext == str(cedula)))
        ).scalars().all()

    await _imprimir_plan(cedula, ciudadano, documentos, transferencias, entradas_outbox)

    for documento in documentos:
        try:
            eliminar_objeto(clave=documento.s3_key)
        except FalloAlmacenamiento as exc:
            print(f"  aviso: no se pudo borrar el objeto {documento.s3_key}: {exc}")

    async with SessionLocal() as session:
        await session.execute(delete(Outbox).where(Outbox.payload["cedula"].astext == str(cedula)))
        await session.execute(delete(Documento).where(Documento.ciudadano_id == cedula))
        await session.execute(delete(Transferencia).where(Transferencia.ciudadano_id == cedula))
        await session.commit()
    print("documento, transferencia y outbox borrados localmente")

    gov = GovCarpeta()
    try:
        await gov.desligar_ciudadano(cedula)
        print("unregisterCitizen: el centralizador confirmo el desligue")
    except Exception as exc:
        print(f"aviso: unregisterCitizen fallo, se continua con la limpieza local igual: {exc}")
    finally:
        await gov.cerrar()

    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, cedula)
        if ciudadano is not None:
            await session.delete(ciudadano)
            await session.commit()
            print("ciudadano borrado (auditoria conservada, con ciudadano_id en NULL)")
        else:
            print("no habia fila de ciudadano que borrar")

    print("\nListo.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Borra toda huella local y en el centralizador de un ciudadano de prueba."
    )
    parser.add_argument("cedula", type=int, help="Cedula del ciudadano de prueba a limpiar")
    args = parser.parse_args()

    asyncio.run(main(args.cedula))
