"""Prueba de punta a punta de la segunda pasada del portal del ciudadano (AD-11):
notificaciones, perfil, segundo factor, sustituir un documento (CU-10) y el traslado a
otro operador (CU-03) -- todo por las pantallas HTML, simulando un navegador con
`httpx.AsyncClient`.

Corre contra `app-a` viva dentro de `docker-compose.test.yml`, con `mock-centralizador`
haciendo de MinTIC/directorio y `app-b` como destino del traslado (mismo trio que
`scripts/probar_envio_transferencia.py`). Termina con el ciudadano `TRASLADADO` en
app-a -- el segundo tramo (primer acceso con el token real que el traslado deja en
app-b) lo cubre `scripts/probar_portal_primer_acceso.py` por separado, porque el token
en claro solo se puede extraer del log de app-b entre una corrida y otra (igual patron
que `scripts/probar_primer_acceso.py`).

Uso (dentro de docker-compose.test.yml, servicio "prueba-portal-segunda-pasada"):
    python scripts/probar_portal_segunda_pasada.py \
        --base-url http://app-a:8000 \
        --operador-destino-id test-operador-b
"""

from __future__ import annotations

import argparse
import asyncio
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

CEDULA_PRUEBA = 900600801
CODIGO_TOTP_SIMULADO = "000000"

PDF_DE_PRUEBA = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 3 3]>>endobj\n"
    b"trailer<</Size 4/Root 1 0 R>>\n"
    b"%%EOF\n"
)


async def _esperar(descripcion: str, condicion, intentos: int = 60, espera_segundos: float = 2.0):
    for _ in range(intentos):
        valor = await condicion()
        if valor:
            return valor
        await asyncio.sleep(espera_segundos)
    raise TimeoutError(f"tiempo agotado esperando: {descripcion}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--operador-destino-id", required=True)
    parser.add_argument("--cedula", type=int, default=CEDULA_PRUEBA)
    args = parser.parse_args()

    cedula = args.cedula
    fallos: list[str] = []
    password = "ClaveDePortal456"

    async with httpx.AsyncClient(base_url=args.base_url, timeout=15.0, follow_redirects=True) as cliente:
        print(f"1. Registrando cedula {cedula} por el portal (con correo personal real, para el traslado)...")
        r = await cliente.post(
            "/registro",
            data={
                "cedula": str(cedula),
                "nombre": f"Prueba Segunda Pasada {cedula}",
                "direccion": "Calle de la segunda pasada 1",
                "telefono": "+573000000003",
                "email_personal": "segunda.pasada@example.com",
                "password": password,
            },
        )
        if r.status_code != 200 or "Iniciar sesión" not in r.text:
            fallos.append(f"registro: se esperaba terminar en iniciar sesion, llego a {r.url}")
            print(r.text[:2000])

        async def _login_funciona() -> bool:
            resp = await cliente.post("/sesion", data={"usuario": str(cedula), "password": password})
            return resp.status_code == 200 and "Mi carpeta" in resp.text

        print("2. Iniciando sesion (el login no exige estado ACTIVO, solo credenciales)...")
        await _esperar("login exitoso tras el registro", _login_funciona)
        print("   -> sesion iniciada")

        print("3. Subiendo un documento por la carpeta (reintenta hasta que el ciudadano quede ACTIVO)...")

        async def _subir_intento():
            resp = await cliente.post(
                "/carpeta/documentos",
                data={"titulo": "Documento original", "tipo": "OTRO"},
                files={"archivo": ("original.pdf", io.BytesIO(PDF_DE_PRUEBA), "application/pdf")},
            )
            coincidencia = re.search(r"/documentos/([0-9a-fA-F-]{36})", resp.text)
            return coincidencia.group(1) if coincidencia else None

        documento_id = await _esperar("ciudadano ACTIVO (carga aceptada)", _subir_intento)
        print(f"   documento_id={documento_id}")

        print("4. CU-10: sustituyendo el documento por una version nueva...")
        r = await cliente.get(f"/documentos/{documento_id}/sustituir")
        if r.status_code != 200 or "Sustituir" not in r.text:
            fallos.append("form sustituir: no se pudo abrir el formulario")
        r = await cliente.post(
            f"/documentos/{documento_id}/sustituir",
            data={"titulo": "Documento version 2", "tipo": "OTRO"},
            files={"archivo": ("v2.pdf", io.BytesIO(PDF_DE_PRUEBA + b"\x01"), "application/pdf")},
        )
        coincidencia2 = re.search(r"/documentos/([0-9a-fA-F-]{36})", str(r.url))
        if r.status_code != 200 or "Documento version 2" not in r.text:
            fallos.append(f"sustituir: se esperaba terminar en el detalle del documento nuevo, llego a {r.url}")
            print(r.text[:2000])
        nuevo_documento_id = str(r.url).rstrip("/").rsplit("/", 1)[-1]
        print(f"   nuevo documento_id={nuevo_documento_id}")

        r = await cliente.get(f"/documentos/{documento_id}")
        if "ya no está vigente" not in r.text and "REEMPLAZADO" not in r.text and "reemplazado" not in r.text.lower():
            fallos.append("documento original: deberia seguir consultable como reemplazado")

        print("5. CU-17: revisando notificaciones (debe existir al menos la de registro)...")
        r = await cliente.get("/notificaciones")
        if r.status_code != 200 or "Notificaciones" not in r.text:
            fallos.append("notificaciones: se esperaba 200 con el titulo de la pagina")
        coincidencia_notif = re.search(r'/notificaciones/(\d+)/leida"', r.text)
        if not coincidencia_notif:
            fallos.append("notificaciones: se esperaba al menos una notificacion sin leer con boton de marcar")
        else:
            notificacion_id = coincidencia_notif.group(1)
            r = await cliente.post(f"/notificaciones/{notificacion_id}/leida")
            if r.status_code != 200:
                fallos.append(f"marcar notificacion leida: se esperaba 200, llego {r.status_code}")
            r = await cliente.get("/notificaciones?solo_no_leidas=1")
            if f"notificacion-{notificacion_id}" in r.text:
                fallos.append("notificaciones: la marcada como leida no deberia aparecer en solo_no_leidas")

        print("   badge de notificaciones visible desde cualquier pantalla...")
        r = await cliente.get("/notificaciones/contador")
        if r.status_code != 200 or 'id="notificaciones-contador"' not in r.text:
            fallos.append("contador de notificaciones: no respondio el fragmento esperado")

        print("6. Perfil: consultando y actualizando datos de contacto...")
        r = await cliente.get("/perfil")
        if r.status_code != 200 or str(cedula) not in r.text:
            fallos.append("perfil: se esperaba ver la cedula del ciudadano")
        r = await cliente.post("/perfil", data={"direccion": "Nueva Direccion 789", "telefono": "+573001112233", "email_personal": "nuevo.correo@example.com"})
        if r.status_code != 200 or "Nueva Direccion 789" not in r.text:
            fallos.append("perfil: la actualizacion de direccion no se reflejo")
        if "Tus datos se actualizaron correctamente" not in r.text:
            fallos.append("perfil: no se mostro el mensaje de exito")

        print("7. Segundo factor: habilitando y luego deshabilitando...")
        r = await cliente.post("/perfil/totp/iniciar")
        if r.status_code != 200 or "código generado por tu aplicación".lower() not in r.text.lower():
            fallos.append("totp iniciar: se esperaba el formulario de confirmacion con el secreto")
        r = await cliente.post("/perfil/totp/confirmar", data={"codigo": CODIGO_TOTP_SIMULADO})
        if r.status_code != 200 or "El segundo factor quedó habilitado" not in r.text:
            fallos.append("totp confirmar: se esperaba el mensaje de exito de habilitacion")
        r = await cliente.get("/perfil/totp")
        if "está habilitado en tu carpeta" not in r.text.lower():
            fallos.append("totp: se esperaba ver el estado habilitado")

        r = await cliente.get("/perfil")
        if "Habilitado" not in r.text:
            fallos.append("perfil: se esperaba ver la etiqueta 'Habilitado' del segundo factor")

        r = await cliente.post("/perfil/totp/deshabilitar", data={"password": password, "codigo": CODIGO_TOTP_SIMULADO})
        if r.status_code != 200 or "El segundo factor quedó deshabilitado" not in r.text:
            fallos.append("totp deshabilitar: se esperaba el mensaje de exito de baja")

        print("8. CU-03: revisando el directorio de operadores para el traslado...")
        r = await cliente.get("/perfil/traslado")
        if r.status_code != 200 or "trasladar" not in r.text.lower():
            fallos.append("traslado: se esperaba 200 con el formulario o el estado")
        if args.operador_destino_id not in r.text:
            print(f"   ADVERTENCIA: {args.operador_destino_id} todavia no aparece en el directorio, esperando el refresco periodico...")

            async def _operador_visible() -> bool:
                resp = await cliente.get("/perfil/traslado")
                return args.operador_destino_id in resp.text

            await _esperar("operador destino en el directorio", _operador_visible, intentos=15, espera_segundos=2.0)

        print("   probando el rechazo sin la casilla de confirmacion marcada...")
        r = await cliente.post("/perfil/traslado", data={"operador_destino_id": args.operador_destino_id})
        if "Debes marcar la casilla" not in r.text:
            fallos.append("traslado sin confirmar: se esperaba el mensaje pidiendo marcar la casilla")

        print("   solicitando el traslado de verdad (con la casilla marcada)...")
        r = await cliente.post("/perfil/traslado", data={"operador_destino_id": args.operador_destino_id, "confirmar": "on"})
        if r.status_code != 200:
            fallos.append(f"traslado: se esperaba 200 tras confirmar, llego {r.status_code}")
        texto = r.text.lower()
        if not any(frase in texto for frase in ("esperando confirmación", "en proceso", "trasladada")):
            fallos.append("traslado: se esperaba ver el estado en curso tras solicitarlo")
            print(r.text[:2000])

        print("9. Esperando a que el traslado se confirme (bandeja de salida real)...")

        async def _traslado_resuelto() -> str | None:
            resp = await cliente.get("/perfil/traslado")
            if "fue trasladada" in resp.text.lower():
                return "confirmada"
            if "no se pudo completar" in resp.text.lower():
                return "fallida"
            return None

        desenlace = await _esperar("traslado resuelto", _traslado_resuelto, intentos=60, espera_segundos=2.0)
        print(f"   -> {desenlace}")
        if desenlace != "confirmada":
            fallos.append(f"traslado: se esperaba que se confirmara, desenlace fue '{desenlace}'")

        print("10. Confirmando que ya no se puede iniciar sesion con esta cedula...")
        r = await cliente.post("/sesion", data={"usuario": str(cedula), "password": password})
        if "Mi carpeta" in r.text:
            fallos.append("login tras traslado: no deberia poder iniciar sesion")
        if r.status_code != 200 or "trasladada" not in r.text.lower():
            fallos.append("login tras traslado: se esperaba un mensaje explicando que la carpeta ya fue trasladada")

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print(
        f"Prueba de la segunda pasada del portal sin fallos. Cedula {cedula} quedo TRASLADADA a "
        f"{args.operador_destino_id} -- para el primer acceso en el destino, ver "
        "scripts/probar_portal_primer_acceso.py."
    )


if __name__ == "__main__":
    asyncio.run(main())
