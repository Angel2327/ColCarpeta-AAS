"""Prueba de punta a punta del portal del ciudadano (AD-11): registro, inicio de
sesion, carga, listado y descarga de un documento, todo por las pantallas HTML en vez
de la API JSON -- simula un navegador con `httpx.AsyncClient` (formularios por
`data=`, no `json=`; la cookie de sesion la conserva el propio cliente, igual que un
navegador real).

El portal vive en la raiz del dominio (/, /sesion, /registro, /carpeta,
/documentos/{id}, ...). Las rutas viejas bajo /portal/... (donde vivio antes) deben
seguir funcionando via redireccion permanente (308): se verifica al final, sin
`follow_redirects`, para poder inspeccionar el codigo y el encabezado `Location` en
vez de solo llegar al destino.

Corre contra una instancia real y viva (docker-compose.test.yml, servicio "app-a").

Uso (dentro de docker-compose.test.yml, servicio "prueba-portal"):
    python scripts/probar_portal.py \
        --base-url http://app-a:8000 \
        --database-url postgresql://postgres:postgres@postgres-a:5432/colcarpeta_a
"""

from __future__ import annotations

import argparse
import asyncio
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
import httpx  # noqa: E402

CEDULA_PRUEBA = 900500701

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
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--cedula", type=int, default=CEDULA_PRUEBA)
    args = parser.parse_args()

    cedula = args.cedula
    fallos: list[str] = []
    password = "ClaveDelPortal123"

    async def _estado_activo() -> str | None:
        conn = await asyncpg.connect(args.database_url)
        try:
            fila = await conn.fetchrow("SELECT estado FROM ciudadano WHERE id = $1", cedula)
            return fila["estado"] if fila and fila["estado"] == "ACTIVO" else None
        finally:
            await conn.close()

    async with httpx.AsyncClient(base_url=args.base_url, timeout=15.0, follow_redirects=True) as cliente:
        print("0. GET / sin sesion debe llevar a iniciar sesion (antes respondia 404)...")
        r = await cliente.get("/")
        print(f"   -> {r.status_code} (url final: {r.url})")
        if r.status_code != 200 or "Iniciar sesión" not in r.text:
            fallos.append(f"raiz sin sesion: se esperaba terminar en iniciar sesion, llego a {r.url}")

        print(f"1. Registrando cedula {cedula} por el formulario del portal...")
        r = await cliente.post(
            "/registro",
            data={
                "cedula": str(cedula),
                "nombre": f"Prueba Portal {cedula}",
                "direccion": "Calle del portal 1",
                "telefono": "+573000000002",
                "email_personal": "prueba.portal@example.com",
                "password": password,
            },
        )
        print(f"   -> {r.status_code} (url final: {r.url})")
        if r.status_code != 200 or "Iniciar sesión" not in r.text:
            fallos.append(f"registro: se esperaba terminar en la pantalla de inicio de sesion, llego a {r.url}")
            print(r.text[:2000])

        print("2. Esperando a que quede ACTIVO (bandeja de salida real de app-a)...")
        await _esperar("ciudadano ACTIVO", _estado_activo)
        print("   -> ACTIVO")

        print("3. Probando una contrasena incorrecta primero (mensaje en lenguaje claro)...")
        r = await cliente.post("/sesion", data={"usuario": str(cedula), "password": "incorrecta-cualquiera"})
        if "incorrect" not in r.text.lower():
            fallos.append("login con contrasena incorrecta: no se encontro un mensaje de error en lenguaje claro")

        print("4. Iniciando sesion de verdad por el formulario del portal...")
        r = await cliente.post("/sesion", data={"usuario": str(cedula), "password": password})
        print(f"   -> {r.status_code} (url final: {r.url})")
        if r.status_code != 200 or "Mi carpeta" not in r.text:
            fallos.append(f"login: se esperaba terminar en 'Mi carpeta', llego a {r.url}")
            print(r.text[:2000])
        if "colcarpeta_sesion" not in cliente.cookies:
            fallos.append("login: no se fijo la cookie de sesion del portal")

        print("5. GET /carpeta debe mostrar la carpeta vacia...")
        r = await cliente.get("/carpeta")
        if r.status_code != 200 or "Todavía no tienes documentos" not in r.text:
            fallos.append("carpeta vacia: no se encontro el mensaje de carpeta vacia")

        print("6. Subiendo un documento por el formulario de la carpeta...")
        r = await cliente.post(
            "/carpeta/documentos",
            data={"titulo": "Certificado de prueba del portal", "tipo": "OTRO"},
            files={"archivo": ("prueba.pdf", io.BytesIO(PDF_DE_PRUEBA), "application/pdf")},
        )
        print(f"   -> {r.status_code} (url final: {r.url})")
        if r.status_code != 200 or "Certificado de prueba del portal" not in r.text:
            fallos.append("subir documento: no aparece en la carpeta tras subirlo")
            print(r.text[:3000])
        if "El documento se subió correctamente" not in r.text:
            fallos.append("subir documento: no se muestra el mensaje de exito")
        if "Subido por ti" not in r.text:
            fallos.append("subir documento: se esperaba la etiqueta 'Subido por ti' (lo subio el ciudadano)")

        coincidencia = re.search(r"/documentos/([0-9a-fA-F-]{36})", r.text)
        if not coincidencia:
            print("ABORTADO: no se pudo extraer el id del documento subido de la carpeta HTML.")
            sys.exit(1)
        documento_id = coincidencia.group(1)
        print(f"   documento_id={documento_id}")

        print("7. GET del detalle del documento: procedencia y firma en lenguaje de persona...")

        async def _firma_resuelta():
            resp = await cliente.get(f"/documentos/{documento_id}")
            if resp.status_code == 200 and "Validando firma" not in resp.text:
                return resp
            return None

        # La validacion de firma corre en segundo plano (AD-05, outbox); justo despues
        # de subir puede seguir PENDIENTE un instante -- se espera a que se resuelva en
        # vez de asumir que ya termino, igual que se hace mas abajo con "ciudadano ACTIVO".
        r = await _esperar("validacion de firma resuelta", _firma_resuelta, intentos=15, espera_segundos=2.0)
        print(f"   -> {r.status_code}")
        if "Sin firma digital" not in r.text:
            fallos.append("detalle del documento: se esperaba 'Sin firma digital' (el PDF de prueba no esta firmado)")
        if "Autenticación ante GovCarpeta" not in r.text:
            fallos.append("detalle del documento: falta la seccion de autenticacion ante GovCarpeta")

        print("8. Descargando el documento (debe llegar el mismo contenido que se subio)...")
        r = await cliente.get(f"/documentos/{documento_id}/descarga")
        print(f"   -> {r.status_code}, {len(r.content)} bytes")
        if r.status_code != 200 or r.content != PDF_DE_PRUEBA:
            fallos.append("descarga: el contenido descargado no coincide con lo que se subio")

        print("9. Eliminando el documento desde la carpeta (borrado diferido, CU-08)...")
        r = await cliente.post(f"/documentos/{documento_id}/eliminar")
        print(f"   -> {r.status_code} (url final: {r.url})")
        if r.status_code != 200 or "El documento se eliminó de tu carpeta" not in r.text:
            fallos.append("eliminar documento: no se mostro el mensaje de exito esperado")
        if documento_id in r.text:
            fallos.append("eliminar documento: el documento eliminado no deberia seguir apareciendo en el listado")

        print("10. Rutas viejas bajo /portal/... deben redirigir de forma permanente (308)...")
        casos_legado = [
            ("GET", "/portal", "/"),
            ("GET", "/portal/carpeta", "/carpeta"),
            ("GET", "/portal/carpeta?subido=1", "/carpeta?subido=1"),
            ("GET", "/portal/sesion", "/sesion"),
            ("GET", "/portal/registro", "/registro"),
            ("GET", f"/portal/documentos/{documento_id}", f"/documentos/{documento_id}"),
            ("GET", f"/portal/documentos/{documento_id}/descarga", f"/documentos/{documento_id}/descarga"),
            ("GET", "/portal/static/estilos.css", "/static/estilos.css"),
        ]
        for metodo, viejo, nuevo_esperado in casos_legado:
            r = await cliente.request(metodo, viejo, follow_redirects=False)
            ubicacion = r.headers.get("location")
            print(f"   {metodo} {viejo} -> {r.status_code} Location: {ubicacion}")
            if r.status_code != 308:
                fallos.append(f"{viejo}: se esperaba 308 (redireccion permanente que preserva metodo), llego {r.status_code}")
            if ubicacion != nuevo_esperado:
                fallos.append(f"{viejo}: se esperaba Location={nuevo_esperado}, llego {ubicacion}")

        print("11. Cerrando sesion y confirmando que /carpeta vuelve a pedir login...")
        r = await cliente.post("/salir")
        if r.status_code != 200 or "Iniciar sesión" not in r.text:
            fallos.append("cerrar sesion: no se termino en la pantalla de inicio de sesion")

        r = await cliente.get("/carpeta")
        if "Iniciar sesión" not in r.text:
            fallos.append("tras cerrar sesion, /carpeta deberia redirigir a iniciar sesion")

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print(
        "Prueba del portal (raiz, registro, login, subir, listar, ver detalle, descargar, "
        "eliminar, redirecciones /portal/... y salir) sin fallos."
    )


if __name__ == "__main__":
    asyncio.run(main())
