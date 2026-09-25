"""Prueba de punta a punta de CU-07 (buscar y clasificar), CU-08 (eliminar documento no
certificado, borrado diferido), CU-10 (sustituir documento temporal sin perder su
historia), CU-13 (deposito de un documento certificado por una entidad emisora,
incluida su revocacion y reactivacion) y CU-17 (centro de notificaciones), mas
GET/PATCH /api/v1/perfil. Tambien cubre el rechazo de CU-11 sobre un documento que ya
no esta vigente (REEMPLAZADO o ELIMINADO).

Corre contra una instancia real y viva (docker-compose.test.yml, servicio "app-a") para
poder verificar tambien la notificacion de registro (CU-01, paso 9), que ya deja algo
en la bandeja de CU-17 sin tener que fabricarla a mano.

Uso (dentro de docker-compose.test.yml, servicio "prueba-carpeta-completa"):
    python scripts/probar_carpeta_completa.py \
        --base-url http://app-a:8000 \
        --database-url postgresql://postgres:postgres@postgres-a:5432/colcarpeta_a

La entidad emisora de prueba se siembra directamente en la base (como
scripts/alta_entidad_emisora.py, pero inline para que la prueba sea reproducible sin
un paso manual aparte) -- no existe una ruta publica para darla de alta.

Al final, prueba tambien `_purgar_documentos` en aislamiento (sin esperar
PURGE_DELAY_DAYS de verdad): retrocede `purgar_despues_de` del documento eliminado
directamente en la base y corre la funcion una sola vez -- mismo patron que
scripts/probar_reconciliacion.py para `_reconciliar_transferencias`.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import uuid as uuid_module
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
import httpx  # noqa: E402

CEDULA_PRUEBA = 900300401

# CU-05 solo admite application/pdf, image/jpeg o image/png, detectado por los primeros
# bytes reales del archivo (app/documentos/tipos.py), no por el nombre.
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
    password = "ClaveDeCarpeta123!"

    async def _estado_activo() -> str | None:
        conn = await asyncpg.connect(args.database_url)
        try:
            fila = await conn.fetchrow("SELECT estado FROM ciudadano WHERE id = $1", cedula)
            return fila["estado"] if fila and fila["estado"] == "ACTIVO" else None
        finally:
            await conn.close()

    async with httpx.AsyncClient(base_url=args.base_url, timeout=15.0) as cliente:
        print(f"1. Registrando cedula {cedula}...")
        r = await cliente.post(
            "/api/v1/registro",
            json={
                "cedula": cedula,
                "nombre": f"Prueba Carpeta Completa {cedula}",
                "direccion": "Calle de prueba 1",
                "email_personal": "carpeta.completa@example.com",
                "telefono": "+573000000001",
                "password": password,
            },
        )
        print(f"   -> {r.status_code} {r.text}")
        if r.status_code != 201:
            print("ABORTADO: el registro no respondio 201.")
            sys.exit(1)

        print("2. Esperando a que quede ACTIVO...")
        await _esperar("ciudadano ACTIVO", _estado_activo)
        print("   -> ACTIVO")

        print("3. Iniciando sesion...")
        r = await cliente.post("/api/v1/sesion", json={"usuario": str(cedula), "password": password})
        if r.status_code != 200:
            print(f"ABORTADO: no se pudo iniciar sesion: {r.text}")
            sys.exit(1)
        cliente.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

        print("4. GET /perfil inicial...")
        r = await cliente.get("/api/v1/perfil")
        perfil = r.json()
        print(f"   -> {r.status_code} {perfil}")
        if perfil["usado_bytes"] != 0:
            fallos.append(f"perfil inicial: se esperaba usado_bytes=0, llego {perfil['usado_bytes']}")
        if perfil["segundo_factor_habilitado"] is not False:
            fallos.append("perfil inicial: se esperaba segundo_factor_habilitado=False")
        email_carpeta = perfil["email_carpeta"]

        print("5. Cargando doc1 (ACADEMICO, Universidad Test)...")
        r = await cliente.post(
            "/api/v1/documentos",
            data={"titulo": "Diploma de prueba", "tipo": "ACADEMICO", "entidad_emisora": "Universidad Test"},
            files={"archivo": ("doc1.pdf", io.BytesIO(PDF_DE_PRUEBA), "application/pdf")},
        )
        print(f"   -> {r.status_code} {r.json()}")
        if r.status_code != 201:
            fallos.append("doc1: se esperaba 201")
        doc1 = r.json()

        print("6. Cargando doc2 (LABORAL, Empresa Test)...")
        r = await cliente.post(
            "/api/v1/documentos",
            data={"titulo": "Certificado laboral de prueba", "tipo": "LABORAL", "entidad_emisora": "Empresa Test"},
            files={"archivo": ("doc2.pdf", io.BytesIO(PDF_DE_PRUEBA + b"\x00"), "application/pdf")},
        )
        print(f"   -> {r.status_code} {r.json()}")
        if r.status_code != 201:
            fallos.append("doc2: se esperaba 201")
        doc2 = r.json()

        print("7. GET /perfil: usado_bytes debe reflejar doc1+doc2...")
        r = await cliente.get("/api/v1/perfil")
        perfil = r.json()
        esperado = doc1["tamano_bytes"] + doc2["tamano_bytes"]
        print(f"   -> usado_bytes={perfil['usado_bytes']} (esperado {esperado})")
        if perfil["usado_bytes"] != esperado:
            fallos.append(f"perfil tras cargar: se esperaba usado_bytes={esperado}, llego {perfil['usado_bytes']}")

        print("8. CU-07: filtros de busqueda y clasificacion...")
        r = await cliente.get("/api/v1/documentos", params={"tipo": "ACADEMICO"})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   tipo=ACADEMICO -> {ids}")
        if ids != {doc1["id"]}:
            fallos.append(f"filtro tipo=ACADEMICO: se esperaba solo doc1, llego {ids}")

        r = await cliente.get("/api/v1/documentos", params={"entidad": "Empresa"})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   entidad=Empresa -> {ids}")
        if ids != {doc2["id"]}:
            fallos.append(f"filtro entidad=Empresa: se esperaba solo doc2, llego {ids}")

        r = await cliente.get("/api/v1/documentos", params={"q": "laboral"})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   q=laboral -> {ids}")
        if ids != {doc2["id"]}:
            fallos.append(f"filtro q=laboral: se esperaba solo doc2, llego {ids}")

        print("   ...variantes de los filtros de texto: parcial, mayusculas, espacios, %...")
        r = await cliente.get("/api/v1/documentos", params={"tipo": "academic"})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   tipo=academic (parcial, minusculas) -> {ids}")
        if ids != {doc1["id"]}:
            fallos.append(f"filtro tipo=academic: se esperaba solo doc1 (parcial, sin distinguir mayusculas), llego {ids}")

        r = await cliente.get("/api/v1/documentos", params={"entidad": "  EMPRESA  "})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   entidad='  EMPRESA  ' (mayusculas + espacios sobrantes) -> {ids}")
        if ids != {doc2["id"]}:
            fallos.append(f"filtro entidad='  EMPRESA  ': se esperaba solo doc2, llego {ids}")

        r = await cliente.get("/api/v1/documentos", params={"tipo": "   "})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   tipo='   ' (solo espacios, debe tratarse como sin filtro) -> {ids}")
        if ids != {doc1["id"], doc2["id"]}:
            fallos.append(f"filtro tipo='   ': un valor de solo espacios deberia equivaler a no filtrar, llego {ids}")

        r = await cliente.get("/api/v1/documentos", params={"q": "%"})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   q='%' (literal, no comodin) -> {ids}")
        if ids:
            fallos.append(f"filtro q='%': ningun titulo tiene un '%' literal, se esperaba vacio, llego {ids}")

        print("   ...acentos (extension unaccent, migracion 6c5a3c89d46e)...")
        r = await cliente.post(
            "/api/v1/documentos",
            data={"titulo": "Documento con acentos de prueba", "tipo": "Académico", "entidad_emisora": "Bufete Muñoz"},
            files={"archivo": ("doc4.pdf", io.BytesIO(PDF_DE_PRUEBA + b"\x01"), "application/pdf")},
        )
        if r.status_code != 201:
            fallos.append(f"doc4 (con acentos): se esperaba 201, llego {r.status_code} {r.text}")
        doc4 = r.json()

        r = await cliente.get("/api/v1/documentos", params={"entidad": "munoz"})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   entidad=munoz (sin acento, minusculas) -> {ids}")
        if ids != {doc4["id"]}:
            fallos.append(f"filtro entidad=munoz: 'munoz' deberia encontrar 'Bufete Muñoz', llego {ids}")

        r = await cliente.get("/api/v1/documentos", params={"tipo": "academico"})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   tipo=academico (sin acento, minusculas) -> {ids}")
        if ids != {doc1["id"], doc4["id"]}:
            fallos.append(f"filtro tipo=academico: deberia encontrar 'ACADEMICO' (doc1) y 'Académico' (doc4), llego {ids}")

        r = await cliente.get("/api/v1/documentos", params={"tipo": "ACADÉMICO"})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   tipo=ACADÉMICO (con acento, mayusculas) -> {ids}")
        if ids != {doc1["id"], doc4["id"]}:
            fallos.append(f"filtro tipo=ACADÉMICO: deberia encontrar tanto 'ACADEMICO' (doc1) como 'Académico' (doc4), llego {ids}")

        # Una busqueda sin acentos para un valor sin acentos debe seguir funcionando
        # exactamente igual que antes de agregar unaccent (regresion, no solo caso nuevo).
        r = await cliente.get("/api/v1/documentos", params={"entidad": "empresa"})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   entidad=empresa (sin acentos, ninguno de los datos los tiene) -> {ids}")
        if ids != {doc2["id"]}:
            fallos.append(f"filtro entidad=empresa: una busqueda sin acentos dejo de comportarse como antes, llego {ids}")

        print("   eliminando doc4 (solo era para probar acentos, no debe afectar la cuota de aqui en adelante)...")
        r = await cliente.delete(f"/api/v1/documentos/{doc4['id']}")
        if r.status_code != 204:
            fallos.append(f"eliminar doc4: se esperaba 204, llego {r.status_code}")

        r = await cliente.get("/api/v1/documentos", params={"certificado": "false"})
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   certificado=false -> {len(ids)} documento(s)")
        if not {doc1["id"], doc2["id"]} <= ids:
            fallos.append("filtro certificado=false: deberia incluir doc1 y doc2")

        r = await cliente.get("/api/v1/documentos", params={"estado_autenticacion": "NO_SOLICITADA"})
        ids = {d["id"] for d in r.json()["items"]}
        if not {doc1["id"], doc2["id"]} <= ids:
            fallos.append("filtro estado_autenticacion=NO_SOLICITADA: deberia incluir doc1 y doc2")

        print("9. CU-10: sustituyendo doc1 (temporal) por doc3 (nueva version)...")
        r = await cliente.post(
            "/api/v1/documentos",
            data={
                "titulo": "Diploma version final de prueba",
                "tipo": "ACADEMICO",
                "entidad_emisora": "Universidad Test",
                "sustituye_a": doc1["id"],
            },
            files={"archivo": ("doc3.pdf", io.BytesIO(PDF_DE_PRUEBA + b"\x01"), "application/pdf")},
        )
        print(f"   -> {r.status_code} {r.json()}")
        if r.status_code != 201:
            fallos.append("doc3 (sustitucion): se esperaba 201")
        doc3 = r.json()
        if doc3.get("sustituye_a") != doc1["id"]:
            fallos.append(f"doc3.sustituye_a: se esperaba {doc1['id']}, llego {doc3.get('sustituye_a')}")

        print("10. doc1 debe seguir consultable (REEMPLAZADO), pero fuera del listado...")
        r = await cliente.get(f"/api/v1/documentos/{doc1['id']}")
        print(f"   -> {r.status_code} estado={r.json().get('estado')}")
        if r.status_code != 200 or r.json().get("estado") != "REEMPLAZADO":
            fallos.append("doc1 deberia seguir consultable con estado=REEMPLAZADO")

        r = await cliente.get("/api/v1/documentos")
        ids = {d["id"] for d in r.json()["items"]}
        print(f"   listado tras sustitucion -> {ids}")
        if doc1["id"] in ids:
            fallos.append("doc1 (REEMPLAZADO) no deberia aparecer en el listado")
        if doc3["id"] not in ids:
            fallos.append("doc3 deberia aparecer en el listado")

        print("11. usado_bytes ya no debe contar doc1, si doc2 y doc3...")
        r = await cliente.get("/api/v1/perfil")
        perfil = r.json()
        esperado = doc2["tamano_bytes"] + doc3["tamano_bytes"]
        print(f"   -> usado_bytes={perfil['usado_bytes']} (esperado {esperado})")
        if perfil["usado_bytes"] != esperado:
            fallos.append(f"usado_bytes tras sustitucion: se esperaba {esperado}, llego {perfil['usado_bytes']}")

        print("12. CU-08: eliminando doc2 (borrado diferido)...")
        r = await cliente.delete(f"/api/v1/documentos/{doc2['id']}")
        print(f"   -> {r.status_code}")
        if r.status_code != 204:
            fallos.append("DELETE doc2: se esperaba 204")

        r = await cliente.get(f"/api/v1/documentos/{doc2['id']}")
        print(f"   GET doc2 tras eliminar -> {r.status_code}")
        if r.status_code != 404:
            fallos.append("doc2 eliminado deberia responder 404")

        r = await cliente.delete(f"/api/v1/documentos/{doc2['id']}")
        print(f"   DELETE doc2 de nuevo -> {r.status_code}")
        if r.status_code != 404:
            fallos.append("DELETE doc2 dos veces deberia responder 404 la segunda vez")

        print("13. CU-08 no debe permitir eliminar un documento ya REEMPLAZADO (doc1)...")
        r = await cliente.delete(f"/api/v1/documentos/{doc1['id']}")
        print(f"   -> {r.status_code} {r.text}")
        if r.status_code != 409:
            fallos.append(f"DELETE doc1 (REEMPLAZADO): se esperaba 409, llego {r.status_code}")

        print("14. CU-11 no debe permitir pedir autenticacion de un documento ya no vigente...")
        r = await cliente.post(f"/api/v1/documentos/{doc1['id']}/autenticacion")
        print(f"   doc1 (REEMPLAZADO) -> {r.status_code} {r.text}")
        if r.status_code != 409:
            fallos.append(f"autenticacion sobre doc1 (REEMPLAZADO): se esperaba 409, llego {r.status_code}")

        r = await cliente.post(f"/api/v1/documentos/{doc2['id']}/autenticacion")
        print(f"   doc2 (ELIMINADO) -> {r.status_code} {r.text}")
        if r.status_code != 404:
            fallos.append(f"autenticacion sobre doc2 (ELIMINADO): se esperaba 404, llego {r.status_code}")

        print("15. CU-13: sembrando una entidad emisora de prueba directamente en la base...")
        from app.db import SessionLocal as _SessionLocal
        from app.identidad.token_acceso import generar_token
        from app.models import EntidadEmisora

        entidad_id = f"entidad-prueba-{cedula}"
        clave_api, clave_api_hash = generar_token()
        async with _SessionLocal() as session:
            existente = await session.get(EntidadEmisora, entidad_id)
            if existente is not None:
                await session.delete(existente)
                await session.commit()
        async with _SessionLocal() as session:
            session.add(EntidadEmisora(id=entidad_id, nombre="Universidad Certificadora de Prueba", api_key_hash=clave_api_hash))
            await session.commit()
        print(f"   -> entidad {entidad_id}")

        r = await cliente.get("/api/v1/perfil")
        usado_antes_doc5 = r.json()["usado_bytes"]

        print("16. CU-13: la entidad deposita un documento certificado (sin sustitucion)...")
        r = await cliente.post(
            "/api/v1/entidades/documentos",
            headers={"X-Api-Key": clave_api},
            data={"cedula": str(cedula), "titulo": "Certificado de prueba", "tipo": "OTRO"},
            files={"archivo": ("doc5.pdf", io.BytesIO(PDF_DE_PRUEBA + b"\x02"), "application/pdf")},
        )
        print(f"   -> {r.status_code} {r.json()}")
        if r.status_code != 201:
            fallos.append("doc5 (deposito de entidad): se esperaba 201")
        doc5 = r.json()
        if doc5.get("certificado") is not True:
            fallos.append("doc5: se esperaba certificado=true")
        if doc5.get("entidad_emisora") != "Universidad Certificadora de Prueba":
            fallos.append(f"doc5.entidad_emisora: se esperaba el nombre de la entidad autenticada, llego {doc5.get('entidad_emisora')}")

        r = await cliente.get("/api/v1/perfil")
        usado_despues_doc5 = r.json()["usado_bytes"]
        print(f"   usado_bytes antes={usado_antes_doc5} despues de depositar doc5 (certificado)={usado_despues_doc5}")
        if usado_despues_doc5 != usado_antes_doc5:
            fallos.append(
                f"depositar un documento certificado no deberia afectar la cuota: "
                f"antes={usado_antes_doc5}, despues={usado_despues_doc5}"
            )

        print("17. CU-13 + CU-10: la entidad deposita un documento certificado que sustituye a doc3 (temporal)...")
        r = await cliente.post(
            "/api/v1/entidades/documentos",
            headers={"X-Api-Key": clave_api},
            data={"cedula": str(cedula), "titulo": "Diploma certificado de verdad", "tipo": "ACADEMICO", "sustituye_a": doc3["id"]},
            files={"archivo": ("doc6.pdf", io.BytesIO(PDF_DE_PRUEBA + b"\x03"), "application/pdf")},
        )
        print(f"   -> {r.status_code} {r.json()}")
        if r.status_code != 201:
            fallos.append("doc6 (deposito de entidad, sustituyendo doc3): se esperaba 201")
        doc6 = r.json()
        if doc6.get("sustituye_a") != doc3["id"]:
            fallos.append(f"doc6.sustituye_a: se esperaba {doc3['id']}, llego {doc6.get('sustituye_a')}")

        r = await cliente.get(f"/api/v1/documentos/{doc3['id']}")
        print(f"   doc3 tras ser sustituido -> {r.status_code} estado={r.json().get('estado')}")
        if r.status_code != 200 or r.json().get("estado") != "REEMPLAZADO":
            fallos.append("doc3 deberia seguir consultable con estado=REEMPLAZADO tras la sustitucion de la entidad")

        r = await cliente.get("/api/v1/perfil")
        usado_final = r.json()["usado_bytes"]
        print(f"   usado_bytes tras sustituir el ultimo temporal por uno certificado -> {usado_final} (esperado 0)")
        if usado_final != 0:
            fallos.append(f"usado_bytes deberia ser 0 (ya no queda ningun temporal ACTIVO), llego {usado_final}")

        print("18. CU-08 no debe permitir eliminar documentos certificados por esta via...")
        r = await cliente.delete(f"/api/v1/documentos/{doc5['id']}")
        print(f"   DELETE doc5 (certificado) -> {r.status_code}")
        if r.status_code != 409:
            fallos.append(f"DELETE doc5 (certificado): se esperaba 409, llego {r.status_code}")
        r = await cliente.delete(f"/api/v1/documentos/{doc6['id']}")
        print(f"   DELETE doc6 (certificado, via sustitucion) -> {r.status_code}")
        if r.status_code != 409:
            fallos.append(f"DELETE doc6 (certificado): se esperaba 409, llego {r.status_code}")

        print("19. Revocando la entidad emisora: debe dejar de autenticarse de inmediato...")
        from app.models import EstadoEntidadEmisora

        async with _SessionLocal() as session:
            entidad_row = await session.get(EntidadEmisora, entidad_id)
            entidad_row.estado = EstadoEntidadEmisora.REVOCADA
            await session.commit()

        r = await cliente.post(
            "/api/v1/entidades/documentos",
            headers={"X-Api-Key": clave_api},
            data={"cedula": str(cedula), "titulo": "No deberia depositarse", "tipo": "OTRO"},
            files={"archivo": ("doc7.pdf", io.BytesIO(PDF_DE_PRUEBA + b"\x04"), "application/pdf")},
        )
        print(f"   deposito con la entidad revocada (misma clave de siempre) -> {r.status_code} {r.text}")
        if r.status_code != 401:
            fallos.append(f"deposito con entidad revocada: se esperaba 401, llego {r.status_code}")

        print("20. Reactivando la entidad con la MISMA clave: debe volver a autenticarse...")
        async with _SessionLocal() as session:
            entidad_row = await session.get(EntidadEmisora, entidad_id)
            entidad_row.estado = EstadoEntidadEmisora.ACTIVA
            await session.commit()

        r = await cliente.post(
            "/api/v1/entidades/documentos",
            headers={"X-Api-Key": clave_api},
            data={"cedula": str(cedula), "titulo": "Certificado tras reactivar", "tipo": "OTRO"},
            files={"archivo": ("doc7.pdf", io.BytesIO(PDF_DE_PRUEBA + b"\x04"), "application/pdf")},
        )
        print(f"   deposito tras reactivar (misma clave, sin rotar) -> {r.status_code} {r.json() if r.status_code == 201 else r.text}")
        if r.status_code != 201:
            fallos.append(f"deposito tras reactivar la entidad: se esperaba 201, llego {r.status_code}")

        print("21. CU-17: la entidad tambien notifica al ciudadano al depositar...")
        r = await cliente.get("/api/v1/notificaciones", params={"size": 50})
        asuntos = [n["asunto"] for n in r.json()["items"]]
        print(f"   -> asuntos en la bandeja: {asuntos}")
        if not any("Universidad Certificadora de Prueba" in a for a in asuntos):
            fallos.append("se esperaba una notificacion mencionando a la entidad que deposito el documento")

        print("22. CU-17: centro de notificaciones (debe existir la de registro, CU-01 paso 9)...")
        r = await cliente.get("/api/v1/notificaciones")
        cuerpo = r.json()
        print(f"   -> {r.status_code} total={cuerpo['total']} no_leidas={cuerpo['no_leidas']}")
        notifs = cuerpo["items"]
        if not notifs:
            fallos.append("se esperaba al menos una notificacion (la de registro)")
        else:
            primera = notifs[0]
            r = await cliente.post(f"/api/v1/notificaciones/{primera['id']}/leida")
            print(f"   marcar leida {primera['id']} -> {r.status_code}")
            if r.status_code != 204:
                fallos.append("marcar notificacion como leida: se esperaba 204")

            r = await cliente.get("/api/v1/notificaciones", params={"solo_no_leidas": "true"})
            ids_no_leidas = {n["id"] for n in r.json()["items"]}
            print(f"   no leidas tras marcar -> {ids_no_leidas}")
            if primera["id"] in ids_no_leidas:
                fallos.append("la notificacion marcada como leida no deberia salir en solo_no_leidas")

        print("23. PATCH /perfil: actualizando telefono y direccion...")
        r = await cliente.patch(
            "/api/v1/perfil", json={"telefono": "+573009999999", "direccion": "Nueva Direccion 456"}
        )
        cuerpo = r.json()
        print(f"   -> {r.status_code} {cuerpo}")
        if r.status_code != 200 or cuerpo["telefono"] != "+573009999999":
            fallos.append("PATCH /perfil: no se aplico el cambio de telefono")
        if cuerpo["direccion"] != "Nueva Direccion 456":
            fallos.append("PATCH /perfil: no se aplico el cambio de direccion")
        if cuerpo["email_carpeta"] != email_carpeta:
            fallos.append("PATCH /perfil: email_carpeta no deberia cambiar, es inmutable (AD-10)")

    print("\n24. Probando _purgar_documentos en aislamiento (doc2 ELIMINADO, sin esperar PURGE_DELAY_DAYS)...")
    from app.db import SessionLocal
    from app.interoperabilidad.outbox import _purgar_documentos
    from app.models import Documento

    doc2_id = uuid_module.UUID(doc2["id"])
    async with SessionLocal() as session:
        documento = await session.get(Documento, doc2_id)
        documento.purgar_despues_de = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    purgados = await _purgar_documentos(SessionLocal)
    print(f"   -> purgados: {purgados}")
    if purgados < 1:
        fallos.append("_purgar_documentos: se esperaba purgar al menos doc2")

    async with SessionLocal() as session:
        documento = await session.get(Documento, doc2_id)
        print(f"   doc2 sigue en la base: {documento is not None}")
        if documento is not None:
            fallos.append("doc2 deberia haber sido purgado (fila borrada)")

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print("Prueba de carpeta completa (CU-07/08/10/11/13/17 + perfil) sin fallos.")


if __name__ == "__main__":
    asyncio.run(main())
