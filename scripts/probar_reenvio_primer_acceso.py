"""Prueba el reenvio del token de primer acceso: siembra un ciudadano pendiente de
primer acceso con un token YA vencido, confirma que ese token no sirve, pide el
reenvio, y confirma que el token viejo SIGUE sin servir despues del reenvio (nunca
llega a mezclarse con el nuevo).

No espera PRIMER_ACCESO_TOKEN_TTL_HORAS de verdad: el token viejo se siembra
directamente en la base ya vencido.

Esta es la primera mitad de la prueba completa. Tras correr este script, hay que
extraer el token NUEVO del log del servidor (el "correo simulado" de
app/notificaciones/correo.py, igual que en el flujo original) y pasarselo a
scripts/probar_primer_acceso.py con --usuario=<la cedula que este script imprime> para
completar el ciclo (establecer la contrasena con el token nuevo e iniciar sesion).

Corre dentro de docker-compose.test.yml (servicio "prueba-reenvio-primer-acceso"),
contra una instancia con su bandeja de salida corriendo (normalmente app-b). No
necesita un ciudadano llegado por una transferencia real: sembrar el estado
directamente alcanza para probar el reenvio en si mismo.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.identidad.token_acceso import hash_token  # noqa: E402
from app.models import Ciudadano, EstadoCiudadano  # noqa: E402

CEDULA = 900400801
TOKEN_VIEJO = "token-viejo-ya-vencido-de-prueba"
EMAIL_PERSONAL = "reenvio.prueba@example.com"


async def _sembrar() -> None:
    async with SessionLocal() as session:
        existente = await session.get(Ciudadano, CEDULA)
        if existente is not None:
            await session.delete(existente)
            await session.commit()
    async with SessionLocal() as session:
        session.add(
            Ciudadano(
                id=CEDULA,
                nombre="Prueba Reenvio Primer Acceso",
                direccion="",
                email_carpeta=f"reenvio.{CEDULA}@carpetacolombia.co",
                email_personal=EMAIL_PERSONAL,
                telefono="",
                password_hash=None,
                estado=EstadoCiudadano.ACTIVO,
                identidad_verificada=True,
                token_primer_acceso_hash=hash_token(TOKEN_VIEJO),
                token_primer_acceso_vence_en=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        )
        await session.commit()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()

    fallos: list[str] = []

    print("1. Sembrando un ciudadano pendiente de primer acceso con un token YA vencido...")
    await _sembrar()
    print(f"   -> cedula {CEDULA}, email_personal={EMAIL_PERSONAL}")

    async with httpx.AsyncClient(base_url=args.base_url, timeout=15.0) as cliente:
        print("2. Confirmando que el token viejo (vencido) no sirve...")
        r = await cliente.post("/api/v1/primer-acceso", json={"token": TOKEN_VIEJO, "password": "ClaveDeReenvio123!"})
        print(f"   -> {r.status_code} {r.text}")
        if r.status_code != 400:
            fallos.append(f"se esperaba 400 con el token vencido, llego {r.status_code}")

        print("3. Solicitando el reenvio (usuario = cedula)...")
        r2 = await cliente.post("/api/v1/primer-acceso/reenviar", json={"usuario": str(CEDULA)})
        print(f"   -> {r2.status_code} {r2.text}")
        if r2.status_code != 202:
            fallos.append(f"se esperaba 202 al reenviar, llego {r2.status_code}")

        print("4. Confirmando que el token VIEJO sigue sin servir despues del reenvio...")
        r3 = await cliente.post("/api/v1/primer-acceso", json={"token": TOKEN_VIEJO, "password": "ClaveDeReenvio123!"})
        print(f"   -> {r3.status_code} {r3.text}")
        if r3.status_code != 400:
            fallos.append(f"se esperaba 400 reusando el token viejo tras el reenvio, llego {r3.status_code}")

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print(
        f"Primera mitad sin fallos. Ahora: extrae el token nuevo del log del servidor y corre\n"
        f"scripts/probar_primer_acceso.py --base-url={args.base_url} --token=<extraido> --usuario={CEDULA}"
    )


if __name__ == "__main__":
    asyncio.run(main())
