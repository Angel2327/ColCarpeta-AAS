"""Prueba aislada de la política de colisión de `email_carpeta` entre dos ciudadanos
DISTINTOS (docs/especificacion.md, "Interoperabilidad entre operadores" > "Colisión de
`email_carpeta` entre dos ciudadanos distintos"; AD-10).

Siembra un ciudadano A ya afiliado a ColCarpeta con una `email_carpeta` conocida, y
llama directamente a `_recibir_transferencia` con una transferencia entrante para una
cédula B DISTINTA que trae ese mismo `citizenEmail`. Se espera que:

  - La recepción se rechace con un ValueError cuyo mensaje nombra explícitamente la
    cédula que ya tiene la dirección (no un IntegrityError opaco de Postgres).
  - No se cree ningún ciudadano para la cédula B.
  - El ciudadano A quede completamente intacto.

No pasa por un servidor vivo ni por `outbox`: se llama a la función directamente, con
un payload sin documentos (`documentos: []`), para no depender de descargar nada ni de
un `GovCarpeta` real -- la colisión se detecta en el paso 2, antes de que el código
llegue a tocar el centralizador. Mismo patrón de aislamiento que
scripts/probar_reconciliacion.py.

Corre dentro de docker-compose.test.yml (servicio "prueba-colision-email"), con
DATABASE_URL apuntando a postgres-a. No toca el MinTIC real ni GovCarpeta de verdad.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.interoperabilidad.govcarpeta import GovCarpeta  # noqa: E402
from app.interoperabilidad.outbox import _recibir_transferencia  # noqa: E402
from app.models import Ciudadano, EstadoCiudadano  # noqa: E402

CEDULA_A = 900400501  # ya afiliada a ColCarpeta, dueña legitima de la direccion
CEDULA_B = 900400502  # llega por transferencia con la MISMA direccion -- debe rechazarse
EMAIL_COLISION = "colision.prueba.2026@carpetacolombia.co"


async def _limpiar() -> None:
    async with SessionLocal() as session:
        for cedula in (CEDULA_A, CEDULA_B):
            ciudadano = await session.get(Ciudadano, cedula)
            if ciudadano is not None:
                await session.delete(ciudadano)
        await session.commit()


async def main() -> None:
    fallos: list[str] = []

    print("Limpiando estado previo...")
    await _limpiar()

    print(f"1. Sembrando el ciudadano A ({CEDULA_A}), ya afiliado con {EMAIL_COLISION}...")
    async with SessionLocal() as session:
        session.add(
            Ciudadano(
                id=CEDULA_A,
                nombre="Colision Prueba A",
                direccion="Calle A 123",
                email_carpeta=EMAIL_COLISION,
                email_personal="colision.a@example.com",
                telefono="",
                password_hash=None,
                estado=EstadoCiudadano.ACTIVO,
                identidad_verificada=True,
            )
        )
        await session.commit()

    print(f"2. Recibiendo una transferencia para la cedula B ({CEDULA_B}) con el MISMO citizenEmail...")
    payload = {
        "cedula": CEDULA_B,
        "nombre": "Colision Prueba B",
        "direccion": "Calle B 456",
        "citizen_email": EMAIL_COLISION,
        "contact_email": "colision.b@example.com",
        "documentos": [],
        "correlation_id": None,
    }
    gov = GovCarpeta()
    error_capturado: Exception | None = None
    try:
        await _recibir_transferencia(gov, payload)
    except ValueError as exc:
        error_capturado = exc
        print(f"   -> rechazada, como se esperaba: {exc}")
    finally:
        await gov.cerrar()

    if error_capturado is None:
        fallos.append("se esperaba que _recibir_transferencia rechazara la recepcion con un ValueError")
    else:
        mensaje = str(error_capturado)
        if str(CEDULA_A) not in mensaje:
            fallos.append(f"el diagnostico deberia nombrar la cedula {CEDULA_A} (la duena actual de la direccion)")
        if EMAIL_COLISION not in mensaje:
            fallos.append(f"el diagnostico deberia nombrar la direccion en colision ({EMAIL_COLISION})")

    print("3. Verificando que no se creo ningun ciudadano para la cedula B...")
    async with SessionLocal() as session:
        creado_b = await session.get(Ciudadano, CEDULA_B)
    print(f"   -> ciudadano B existe: {creado_b is not None}")
    if creado_b is not None:
        fallos.append("no deberia haberse creado un ciudadano para la cedula B")

    print("4. Verificando que el ciudadano A sigue intacto...")
    async with SessionLocal() as session:
        a = await session.get(Ciudadano, CEDULA_A)
    if a is None:
        fallos.append("el ciudadano A no deberia haberse tocado, y ya no existe")
    else:
        print(f"   -> A: estado={a.estado}, email_carpeta={a.email_carpeta}")
        if a.estado != EstadoCiudadano.ACTIVO or a.email_carpeta != EMAIL_COLISION:
            fallos.append("el ciudadano A no deberia haber cambiado de estado ni de email_carpeta")

    print("\nLimpiando datos de prueba...")
    await _limpiar()

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print("Colision de email_carpeta entre dos ciudadanos distintos probada sin fallos.")


if __name__ == "__main__":
    asyncio.run(main())
