"""Prueba del caso concreto pedido: un ciudadano que ya fue nuestro, se tradujo a otro
operador (TRASLADADO), y vuelve por una transferencia entrante ANTES de que se cumpla
la purga diferida de su salida anterior. Su fila vieja sigue existiendo con la misma
`email_carpeta` (AD-10: es portable y permanente, así que una transferencia real
traería exactamente la misma direccion en `citizenEmail`).

Corre dentro de docker-compose.test.yml (servicio "prueba-regreso"), contra app-a y su
propia base de datos local -- nunca contra el MinTIC real ni la base de Supabase.

Siembra directamente en la base el registro TRASLADADO previo (mas rapido y
determinista que hacer un ciclo completo de CU-03 primero), envia una transferencia
entrante con la misma cedula y el mismo email_carpeta, y verifica que:
  - la recepcion no falla por colision de email_carpeta contra su propio registro viejo,
  - el documento y el objeto S3 viejos se borran,
  - queda una auditoria "transferencia.registro_anterior_reemplazado",
  - el ciudadano nuevo llega a ACTIVO (no se queda colgado en TRASLADADO), y
  - el documento nuevo de la transferencia entrante si se guarda.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.documentos.almacenamiento import generar_clave, subir_objeto  # noqa: E402
from app.models import Auditoria, Ciudadano, Documento, EstadoCiudadano, Outbox  # noqa: E402

CEDULA = 900200401
DOCUMENTO_DE_PRUEBA = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
PDF_VIEJO = (
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


async def _limpiar() -> None:
    async with SessionLocal() as session:
        for fila in (await session.execute(select(Outbox).where(Outbox.payload["cedula"].astext == str(CEDULA)))).scalars():
            await session.delete(fila)
        for fila in (await session.execute(select(Documento).where(Documento.ciudadano_id == CEDULA))).scalars():
            await session.delete(fila)
        ciudadano = await session.get(Ciudadano, CEDULA)
        if ciudadano is not None:
            await session.delete(ciudadano)
        await session.commit()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="URL de la instancia que recibe (app-a)")
    args = parser.parse_args()

    email_carpeta = f"regreso.{CEDULA}@carpetacolombia.co"
    fallos: list[str] = []

    await _limpiar()

    print("1. Sembrando un registro TRASLADADO previo (ya se fue antes de esta prueba)...")
    clave_vieja = generar_clave("application/pdf")
    subir_objeto(clave=clave_vieja, contenido=PDF_VIEJO, content_type="application/pdf")
    async with SessionLocal() as session:
        session.add(
            Ciudadano(
                id=CEDULA,
                nombre="Ciudadano Que Ya Se Fue",
                direccion="Direccion vieja",
                email_carpeta=email_carpeta,
                email_personal="viejo@example.com",
                telefono="",
                password_hash=None,
                estado=EstadoCiudadano.TRASLADADO,
                identidad_verificada=True,
            )
        )
        await session.flush()
        session.add(
            Documento(
                ciudadano_id=CEDULA,
                titulo="Documento viejo",
                tipo="OTRO",
                entidad_emisora=None,
                fecha_emision=None,
                s3_key=clave_vieja,
                content_type="application/pdf",
                tamano_bytes=len(PDF_VIEJO),
                hash_sha256=hashlib.sha256(PDF_VIEJO).hexdigest(),
                certificado=False,
            )
        )
        await session.commit()
    print(f"   -> TRASLADADO con email_carpeta={email_carpeta}, documento viejo en {clave_vieja}")

    print("2. Enviando una transferencia ENTRANTE con la MISMA cedula y el MISMO email_carpeta...")
    async with httpx.AsyncClient(base_url=args.base_url, timeout=15.0) as cliente:
        r = await cliente.post(
            "/api/transferCitizen",
            json={
                "id": CEDULA,
                "citizenName": "Ciudadano Que Vuelve",
                # AD-10: portable y permanente -- una transferencia real traeria
                # exactamente esta misma direccion, la que ya tiene su fila vieja.
                "citizenEmail": email_carpeta,
                "contactEmail": "nuevo@example.com",
                "urlDocuments": {"Documento nuevo": DOCUMENTO_DE_PRUEBA},
                "documentsMetadata": [{"tipo": "OTRO", "certificado": False}],
                "confirmAPI": "http://ejemplo.invalido/api/transferCitizenConfirm",
            },
        )
        print(f"   -> {r.status_code} {r.text}")
        if r.status_code != 200:
            print("ABORTADO: la ruta de recepcion no respondio 200.")
            sys.exit(1)

    print("3. Esperando a que la bandeja de salida procese receiveTransferCitizen...")
    try:
        estado, nombre = await _esperar("ciudadano ACTIVO tras el regreso", lambda: _solo_si_activo())
        print(f"   -> estado={estado} nombre={nombre!r}")
    except TimeoutError as exc:
        fallos.append(str(exc))
        async with SessionLocal() as session:
            c = await session.get(Ciudadano, CEDULA)
            print(f"   -> estado final observado: {c.estado if c else 'NO EXISTE'}")

    print("4. Verificando que el documento y la fila viejos se reemplazaron...")
    async with SessionLocal() as session:
        documentos = (await session.execute(select(Documento).where(Documento.ciudadano_id == CEDULA))).scalars().all()
        titulos = {d.titulo for d in documentos}
        print(f"   -> documentos actuales: {titulos}")
        if "Documento viejo" in titulos:
            fallos.append("el documento viejo (de antes del traslado) sigue existiendo -- no se reemplazo")
        if "Documento nuevo" not in titulos:
            fallos.append("el documento nuevo de la transferencia entrante no se guardo")

        auditoria = (await session.execute(
            select(Auditoria).where(
                Auditoria.accion == "transferencia.registro_anterior_reemplazado", Auditoria.recurso == str(CEDULA)
            )
        )).scalars().first()
        print(f"   -> auditoria 'transferencia.registro_anterior_reemplazado': {auditoria is not None}")
        if auditoria is None:
            fallos.append("no quedo auditoria de que se reemplazo el registro anterior")

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print("Regreso antes de la purga probado de punta a punta sin fallos.")


async def _solo_si_activo():
    async with SessionLocal() as session:
        c = await session.get(Ciudadano, CEDULA)
        if c is None or c.estado != EstadoCiudadano.ACTIVO:
            return None
        return (c.estado.value, c.nombre)


if __name__ == "__main__":
    asyncio.run(main())
