"""Prueba de punta a punta de CU-09 (validación de firma digital, vía pyHanko).

Genera con pyHanko un PDF firmado, un PDF firmado y luego alterado, y un PDF sin firma,
y comprueba que:

  - El firmado válida `firma_valida=true`, con `firma_firmante` y `firma_fecha`
    tomados de la propia firma.
  - El alterado después de firmarse valida `firma_valida=false` (detecta que el
    contenido cambió), pero el documento se conserva igual: una firma inválida no
    rechaza la carga.
  - El que no trae firma queda con `firma_valida` sin valor (`None`) para siempre --
    distinguible de `false` (una firma que sí se evaluó y no pasó). Se confirma
    consultando directamente la fila de `outbox`: el trabajo `validarFirma` sí corrió
    (terminó `COMPLETADO`), simplemente no encontró nada que aplicar.

La validación corre en segundo plano (AD-05): cada carga solo encola el trabajo, así
que el script espera a que la bandeja de salida de `app-a` lo procese antes de
verificar el resultado, en vez de asumirlo inmediato.

Cubre también CU-13 (depósito por una entidad emisora): el mismo PDF firmado,
depositado por una entidad de prueba sembrada directamente en la base, valida igual.

Uso (dentro de docker-compose.test.yml, servicio "prueba-firma-digital"):
    python scripts/probar_firma_digital.py \
        --base-url http://app-a:8000 \
        --database-url postgresql://postgres:postgres@postgres-a:5432/colcarpeta_a
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from pyhanko.pdf_utils import generic  # noqa: E402
from pyhanko.pdf_utils.generic import pdf_name  # noqa: E402
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter  # noqa: E402
from pyhanko.pdf_utils.writer import PdfFileWriter  # noqa: E402
from pyhanko.sign import signers  # noqa: E402
from pyhanko.sign.fields import SigFieldSpec, append_signature_field  # noqa: E402
from pyhanko.sign.signers.pdf_cms import SimpleSigner  # noqa: E402

CEDULA_PRUEBA = 900500601
FIRMANTE_CN = "Universidad Certificadora de Prueba - Registrador"


def _pdf_minimo() -> bytes:
    """Un PDF valido (con tabla xref real) y una sola pagina en blanco -- a diferencia
    del PDF_DE_PRUEBA de otros scripts (que no tiene una estructura real y basta para
    pasar el sniffing de content-type), pyHanko necesita poder interpretarlo de verdad
    para poder firmarlo."""
    w = PdfFileWriter()
    pagina = generic.DictionaryObject(
        {
            pdf_name("/Type"): pdf_name("/Page"),
            pdf_name("/MediaBox"): generic.ArrayObject(
                [generic.NumberObject(0), generic.NumberObject(0), generic.NumberObject(200), generic.NumberObject(200)]
            ),
        }
    )
    w.insert_page(pagina)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _firmante_de_prueba():
    """Genera un certificado autofirmado ad-hoc, solo para esta prueba: nunca se
    intenta que sea confiable (no hay autoridad certificadora detras), solo sirve para
    ejercitar la validacion criptografica -- ver app.documentos.firma para el porque no
    se valida la cadena de confianza."""
    llave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, FIRMANTE_CN)])
    certificado = (
        x509.CertificateBuilder()
        .subject_name(nombre)
        .issuer_name(nombre)
        .public_key(llave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
        .sign(llave, hashes.SHA256())
    )
    directorio = tempfile.mkdtemp()
    ruta_llave = os.path.join(directorio, "llave.pem")
    ruta_cert = os.path.join(directorio, "cert.pem")
    with open(ruta_llave, "wb") as f:
        f.write(
            llave.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
            )
        )
    with open(ruta_cert, "wb") as f:
        f.write(certificado.public_bytes(serialization.Encoding.PEM))
    return SimpleSigner.load(ruta_llave, ruta_cert)


async def _firmar_pdf(pdf_bytes: bytes, firmante) -> bytes:
    w = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes))
    append_signature_field(w, SigFieldSpec(sig_field_name="Signature1"))
    salida = io.BytesIO()
    await signers.async_sign_pdf(
        w, signers.PdfSignatureMetadata(field_name="Signature1"), signer=firmante, output=salida
    )
    return salida.getvalue()


def _alterar_despues_de_firmar(pdf_firmado: bytes) -> bytes:
    """Cambia un byte del contenido visible (no de la firma en si) para que el
    documento resultante sea distinto del que se firmo -- eso es lo que `intact` debe
    detectar."""
    alterado = bytearray(pdf_firmado)
    indice = alterado.find(b"200")
    assert indice != -1, "no se encontro el marcador esperado en el PDF de prueba"
    alterado[indice : indice + 3] = b"999"
    return bytes(alterado)


async def _esperar(descripcion: str, condicion, intentos: int = 30, espera_segundos: float = 2.0):
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
    password = "ClaveDeFirma123!"

    print("Generando los PDF de prueba con pyHanko (firmado, alterado, sin firma)...")
    base = _pdf_minimo()
    firmante = _firmante_de_prueba()
    firmado = await _firmar_pdf(base, firmante)
    alterado = _alterar_despues_de_firmar(firmado)
    print(f"   -> base={len(base)}B firmado={len(firmado)}B alterado={len(alterado)}B")

    async def _outbox_completado(document_id: str) -> bool:
        conn = await asyncpg.connect(args.database_url)
        try:
            fila = await conn.fetchrow(
                "SELECT estado FROM outbox WHERE operacion = 'validarFirma' "
                "AND payload->>'documento_id' = $1 ORDER BY id DESC LIMIT 1",
                document_id,
            )
            return fila is not None and fila["estado"] == "COMPLETADO"
        finally:
            await conn.close()

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
                "nombre": f"Prueba Firma Digital {cedula}",
                "direccion": "Calle de prueba 1",
                "email_personal": "firma.digital@example.com",
                "telefono": "+573000000002",
                "password": password,
            },
        )
        if r.status_code != 201:
            print(f"ABORTADO: el registro no respondio 201: {r.text}")
            sys.exit(1)

        print("2. Esperando a que quede ACTIVO...")
        await _esperar("ciudadano ACTIVO", _estado_activo)

        print("3. Iniciando sesion...")
        r = await cliente.post("/api/v1/sesion", json={"usuario": str(cedula), "password": password})
        if r.status_code != 200:
            print(f"ABORTADO: no se pudo iniciar sesion: {r.text}")
            sys.exit(1)
        cliente.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

        print("4. Cargando el PDF firmado (CU-05)...")
        r = await cliente.post(
            "/api/v1/documentos",
            data={"titulo": "Documento firmado de prueba", "tipo": "OTRO"},
            files={"archivo": ("firmado.pdf", io.BytesIO(firmado), "application/pdf")},
        )
        print(f"   -> {r.status_code} {r.json()}")
        if r.status_code != 201:
            fallos.append("carga del PDF firmado: se esperaba 201")
        doc_firmado = r.json()
        if doc_firmado.get("firma_valida") is not None:
            fallos.append("la respuesta inmediata de la carga no deberia traer firma_valida todavia (corre en segundo plano)")

        print("5. Cargando el PDF alterado despues de firmarse...")
        r = await cliente.post(
            "/api/v1/documentos",
            data={"titulo": "Documento alterado de prueba", "tipo": "OTRO"},
            files={"archivo": ("alterado.pdf", io.BytesIO(alterado), "application/pdf")},
        )
        print(f"   -> {r.status_code} {r.json()}")
        if r.status_code != 201:
            fallos.append("carga del PDF alterado: se esperaba 201")
        doc_alterado = r.json()

        print("6. Cargando un PDF sin firma...")
        r = await cliente.post(
            "/api/v1/documentos",
            data={"titulo": "Documento sin firma de prueba", "tipo": "OTRO"},
            files={"archivo": ("sinfirma.pdf", io.BytesIO(base), "application/pdf")},
        )
        print(f"   -> {r.status_code} {r.json()}")
        if r.status_code != 201:
            fallos.append("carga del PDF sin firma: se esperaba 201")
        doc_sin_firma = r.json()

        print("7. Esperando a que la bandeja de salida valide las tres firmas...")
        for doc in (doc_firmado, doc_alterado, doc_sin_firma):
            await _esperar(f"validarFirma completado para {doc['id']}", lambda d=doc: _outbox_completado(d["id"]))

        print("8. Verificando el resultado del PDF firmado (debe ser valido)...")
        r = await cliente.get(f"/api/v1/documentos/{doc_firmado['id']}")
        cuerpo = r.json()
        print(f"   -> firma_valida={cuerpo['firma_valida']} firmante={cuerpo['firma_firmante']} fecha={cuerpo['firma_fecha']}")
        if cuerpo["firma_valida"] is not True:
            fallos.append(f"PDF firmado: se esperaba firma_valida=true, llego {cuerpo['firma_valida']}")
        if not cuerpo["firma_firmante"] or FIRMANTE_CN not in cuerpo["firma_firmante"]:
            fallos.append(f"PDF firmado: se esperaba el firmante '{FIRMANTE_CN}' en firma_firmante, llego {cuerpo['firma_firmante']}")
        if not cuerpo["firma_fecha"]:
            fallos.append("PDF firmado: se esperaba firma_fecha con valor")

        print("9. Verificando el resultado del PDF alterado (debe ser invalido, pero seguir existiendo)...")
        r = await cliente.get(f"/api/v1/documentos/{doc_alterado['id']}")
        cuerpo = r.json()
        print(f"   -> {r.status_code} firma_valida={cuerpo.get('firma_valida')}")
        if r.status_code != 200:
            fallos.append("PDF alterado: el documento deberia seguir existiendo (una firma invalida no lo rechaza)")
        if cuerpo.get("firma_valida") is not False:
            fallos.append(f"PDF alterado: se esperaba firma_valida=false, llego {cuerpo.get('firma_valida')}")

        print("10. Verificando el resultado del PDF sin firma (debe quedar sin valor, no false)...")
        r = await cliente.get(f"/api/v1/documentos/{doc_sin_firma['id']}")
        cuerpo = r.json()
        print(f"   -> firma_valida={cuerpo.get('firma_valida')}")
        if cuerpo.get("firma_valida") is not None:
            fallos.append(f"PDF sin firma: se esperaba firma_valida sin valor (None), llego {cuerpo.get('firma_valida')}")

        print("11. CU-13: sembrando una entidad emisora y depositando el PDF firmado...")
        from app.db import SessionLocal
        from app.identidad.token_acceso import generar_token
        from app.models import EntidadEmisora

        entidad_id = f"entidad-firma-{cedula}"
        clave_api, clave_api_hash = generar_token()
        async with SessionLocal() as session:
            existente = await session.get(EntidadEmisora, entidad_id)
            if existente is not None:
                await session.delete(existente)
                await session.commit()
        async with SessionLocal() as session:
            session.add(EntidadEmisora(id=entidad_id, nombre="Entidad Firma de Prueba", api_key_hash=clave_api_hash))
            await session.commit()

        r = await cliente.post(
            "/api/v1/entidades/documentos",
            headers={"X-Api-Key": clave_api},
            data={"cedula": str(cedula), "titulo": "Certificado firmado de prueba", "tipo": "OTRO"},
            files={"archivo": ("firmado_entidad.pdf", io.BytesIO(firmado), "application/pdf")},
        )
        print(f"   -> {r.status_code} {r.json()}")
        if r.status_code != 201:
            fallos.append("deposito de entidad con PDF firmado: se esperaba 201")
        doc_entidad = r.json()
        if doc_entidad.get("firma_valida") is not None:
            fallos.append("deposito de entidad: la respuesta inmediata no deberia traer firma_valida todavia")

        print("12. Esperando la validacion del deposito de la entidad...")
        await _esperar("validarFirma completado para el deposito de la entidad", lambda: _outbox_completado(doc_entidad["id"]))
        r = await cliente.get(f"/api/v1/documentos/{doc_entidad['id']}")
        cuerpo = r.json()
        print(f"   -> certificado={cuerpo['certificado']} firma_valida={cuerpo['firma_valida']}")
        if cuerpo["firma_valida"] is not True:
            fallos.append(f"deposito de entidad: se esperaba firma_valida=true, llego {cuerpo['firma_valida']}")
        if cuerpo["certificado"] is not True:
            fallos.append("deposito de entidad: se esperaba certificado=true")

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print("Validacion de firma digital (CU-09), en CU-05 y CU-13, probada sin fallos.")


if __name__ == "__main__":
    asyncio.run(main())
