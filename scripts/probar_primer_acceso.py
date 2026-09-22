"""Prueba el establecimiento de la contraseña de primer acceso de un ciudadano
recibido por transferencia: consume el token de un solo uso y confirma que, tras
establecer la contraseña, el ciudadano puede iniciar sesión normalmente.

El token en claro nunca se guarda en la base -- solo su hash -- así que este script no
lo obtiene de la base de datos: lo recibe como argumento, extraído de donde
efectivamente "llegó" en esta entrega (el log del servidor, que hace de bandeja de
salida del correo simulado -- ver app/notificaciones/correo.py). Quien orquesta esta
prueba es responsable de extraerlo de ahí antes de invocar este script.

Corre dentro de docker-compose.test.yml (servicio "prueba-primer-acceso"), contra la
instancia que recibió al ciudadano (normalmente app-b).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--usuario", required=True, help="cedula o email_carpeta para el inicio de sesion posterior")
    parser.add_argument("--password", default="ClaveDePrimerAcceso123!")
    args = parser.parse_args()

    fallos: list[str] = []

    async with httpx.AsyncClient(base_url=args.base_url, timeout=15.0) as cliente:
        print("1. Estableciendo contrasena con el token de primer acceso...")
        r = await cliente.post("/api/v1/primer-acceso", json={"token": args.token, "password": args.password})
        print(f"   -> {r.status_code} {r.text}")
        if r.status_code != 204:
            fallos.append(f"se esperaba 204 al establecer la contrasena, llego {r.status_code}: {r.text}")

        print("2. Reintentando el MISMO token (debe fallar: ya se uso, y se invalido al usarse)...")
        r2 = await cliente.post("/api/v1/primer-acceso", json={"token": args.token, "password": args.password})
        print(f"   -> {r2.status_code} {r2.text}")
        if r2.status_code != 400:
            fallos.append(f"se esperaba 400 al reusar el token, llego {r2.status_code}")

        print("3. Iniciando sesion con la contrasena recien establecida...")
        r3 = await cliente.post("/api/v1/sesion", json={"usuario": args.usuario, "password": args.password})
        print(f"   -> {r3.status_code}")
        if r3.status_code != 200:
            fallos.append(f"se esperaba 200 al iniciar sesion, llego {r3.status_code}: {r3.text}")
        elif "access_token" not in r3.json():
            fallos.append("la respuesta de inicio de sesion no trae access_token")

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print("Primer acceso probado de punta a punta sin fallos.")


if __name__ == "__main__":
    asyncio.run(main())
