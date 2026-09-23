"""Prueba el establecimiento de la contraseña de primer acceso por las pantallas HTML
del portal (`GET`/`POST /primer-acceso`), con un token REAL emitido por una
transferencia -- a diferencia de `scripts/probar_primer_acceso.py`, que ejercita la
misma operación contra la API JSON.

El token en claro nunca se guarda en la base -- solo su hash -- así que este script no
lo obtiene de la base de datos: lo recibe como argumento, extraído de donde
efectivamente "llegó" en esta entrega (el log del servidor, que hace de bandeja de
salida del correo simulado -- ver app/notificaciones/correo.py). Quien orquesta esta
prueba (`scripts/probar_portal_segunda_pasada.py` deja al ciudadano trasladado al
destino, y el token se extrae del log de ese destino) es responsable de extraerlo de
ahí antes de invocar este script.

Corre dentro de docker-compose.test.yml (servicio "prueba-portal-primer-acceso"),
contra la instancia que recibió al ciudadano (normalmente app-b).
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
    parser.add_argument("--password", default="ClaveDePortalPrimerAcceso123")
    args = parser.parse_args()

    fallos: list[str] = []

    async with httpx.AsyncClient(base_url=args.base_url, timeout=15.0, follow_redirects=True) as cliente:
        print("1. Abriendo /primer-acceso con el token en la URL (como llegaria por el enlace del correo)...")
        r = await cliente.get(f"/primer-acceso?token={args.token}")
        if r.status_code != 200 or f'value="{args.token}"' not in r.text:
            fallos.append("form primer-acceso: el token de la URL deberia venir precargado en el campo")

        print("2. Estableciendo la contrasena con el token real por el formulario del portal...")
        r = await cliente.post("/primer-acceso", data={"token": args.token, "password": args.password})
        print(f"   -> {r.status_code} (url final: {r.url})")
        if r.status_code != 200 or "Iniciar sesión" not in r.text:
            fallos.append(f"establecer contrasena: se esperaba terminar en iniciar sesion, llego a {r.url}")
            print(r.text[:2000])
        if "Tu contraseña quedó establecida" not in r.text:
            fallos.append("establecer contrasena: no se mostro el mensaje de exito esperado")

        print("3. Reintentando el MISMO token por el portal (debe fallar: ya se uso)...")
        r2 = await cliente.post("/primer-acceso", data={"token": args.token, "password": args.password})
        if "El enlace no es válido o ya venció" not in r2.text:
            fallos.append("reuso del token: se esperaba el mensaje de enlace invalido, en lenguaje claro")

        print("4. Iniciando sesion por el portal con la contrasena recien establecida...")
        r3 = await cliente.post("/sesion", data={"usuario": args.usuario, "password": args.password})
        print(f"   -> {r3.status_code} (url final: {r3.url})")
        if r3.status_code != 200 or "Mi carpeta" not in r3.text:
            fallos.append(f"login tras primer acceso: se esperaba terminar en 'Mi carpeta', llego a {r3.url}")
            print(r3.text[:2000])
        if "colcarpeta_sesion" not in cliente.cookies:
            fallos.append("login tras primer acceso: no se fijo la cookie de sesion del portal")

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print("Primer acceso por el portal, con un token real de transferencia, probado de punta a punta sin fallos.")


if __name__ == "__main__":
    asyncio.run(main())
