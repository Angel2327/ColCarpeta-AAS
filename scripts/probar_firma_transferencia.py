"""Prueba de punta a punta de CU-09 enganchado en CU-16 ("cuarentena" real, ver
docs/especificacion.md "Documentos recibidos por transferencia (CU-16) y su firma").

Simula ser un operador de origen (como scripts/probar_transferencia.py) que envía una
transferencia con DOS documentos, ambos declarados `certificado: true` en
`documentsMetadata` -- una afirmación del operador de origen, sin autenticar-- :

  - Uno con una firma digital válida (generada con pyHanko).
  - Ese mismo, alterado después de firmarse.

Ambos se sirven desde un servidor HTTP local (ligero, solo librería estándar) que hace
de "almacenamiento del operador de origen", igual que `confirmAPI` en
`probar_transferencia.py`. Se envían contra `app-a` corriendo junto a un
`mock-centralizador` (para que `validateCitizen` diga "disponible" y la recepción
llegue de verdad hasta `ACTIVO` -- con la cédula seguirá de otro operador real la
recepción se descartaría antes de terminar de validar nada).

Verifica, consultando la base de datos directamente (el ciudadano recibido no tiene
contraseña todavía -- primer acceso, CU-16 -- así que no hay por qué pasar por el login
para esta comprobación):

  - Los dos documentos quedan visibles de inmediato (antes de que la bandeja de salida
    alcance a validar la firma): no hay una "cuarentena" que los oculte.
  - El firmado termina con `firma_valida = true` y conserva `certificado = true` (lo
    que declaró el operador de origen, respaldado por una firma real).
  - El alterado termina con `firma_valida = false` y pierde el `certificado` que el
    operador de origen le había puesto (`documento.certificacion_revocada_por_firma_invalida`
    en auditoría) -- pero el documento se conserva, con el resultado visible.
  - El ciudadano recibe una notificación (centro de CU-17) explicando en lenguaje llano
    por qué el documento alterado dejó de estar certificado -- y ninguna notificación
    equivalente por el documento firmado (válido).
  - El documento alterado, ya no certificado, pasa a contar contra la cuota de
    almacenamiento del ciudadano (el firmado, que la conserva, no). No se prueba un
    `CUOTA_AGOTADA` real (los PDF de prueba son minúsculos frente a los 200 MB por
    defecto): se verifica el mecanismo mismo -- la suma que usa `cargar_documento` para
    decidir si hay cupo ya incluye el documento revocado -- que es lo que dispararía el
    rechazo al llegar al límite real.

Uso (dentro de docker-compose.test.yml, servicio "prueba-firma-transferencia"):
    python scripts/probar_firma_transferencia.py \
        --base-url http://app-a:8000 \
        --database-url postgresql://postgres:postgres@postgres-a:5432/colcarpeta_a \
        --puerto-servidor 9401 \
        --servidor-bind 0.0.0.0 \
        --servidor-host prueba-firma-transferencia
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import io
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
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

CEDULA_PRUEBA = 900600701  # ficticia, nunca toca el MinTIC real (mock-centralizador)
FIRMANTE_CN = "Operador Origen (prueba) - Registrador"

# --- generacion de los PDF de prueba (mismo codigo que scripts/probar_firma_digital.py:
# cada script de prueba de este proyecto es autocontenido a proposito, ver ese script) --


def _pdf_minimo() -> bytes:
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
    alterado = bytearray(pdf_firmado)
    indice = alterado.find(b"200")
    assert indice != -1, "no se encontro el marcador esperado en el PDF de prueba"
    alterado[indice : indice + 3] = b"999"
    return bytes(alterado)


# --- servidor local: sirve los dos PDF y recibe confirmAPI, igual patron que
# scripts/probar_transferencia.py --------------------------------------------------


class _Manejador(BaseHTTPRequestHandler):
    firmado: bytes = b""
    alterado: bytes = b""
    confirmacion: dict | None = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/firmado.pdf":
            cuerpo = _Manejador.firmado
        elif self.path == "/alterado.pdf":
            cuerpo = _Manejador.alterado
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_POST(self) -> None:  # noqa: N802
        largo = int(self.headers.get("Content-Length", 0))
        cuerpo = self.rfile.read(largo)
        try:
            _Manejador.confirmacion = json.loads(cuerpo)
        except json.JSONDecodeError:
            _Manejador.confirmacion = {"cuerpo_crudo": cuerpo.decode(errors="replace")}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"received": true}')

    def log_message(self, format: str, *args: object) -> None:
        return


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
    parser.add_argument("--puerto-servidor", type=int, default=9401)
    parser.add_argument("--servidor-bind", default="127.0.0.1")
    parser.add_argument(
        "--servidor-host",
        default=None,
        help="host que se anuncia en las URL de documentos y confirmAPI, si es distinto "
        "de --servidor-bind (obligatorio si --servidor-bind es 0.0.0.0)",
    )
    args = parser.parse_args()

    cedula = args.cedula
    fallos: list[str] = []

    host_anunciado = args.servidor_host or args.servidor_bind
    if host_anunciado == "0.0.0.0":
        print("ERROR: --servidor-bind es 0.0.0.0 pero no se dio --servidor-host.", file=sys.stderr)
        sys.exit(1)

    print("Generando los PDF de prueba con pyHanko (firmado, alterado)...")
    base = _pdf_minimo()
    firmante = _firmante_de_prueba()
    _Manejador.firmado = await _firmar_pdf(base, firmante)
    _Manejador.alterado = _alterar_despues_de_firmar(_Manejador.firmado)
    print(f"   -> firmado={len(_Manejador.firmado)}B alterado={len(_Manejador.alterado)}B")

    servidor = HTTPServer((args.servidor_bind, args.puerto_servidor), _Manejador)
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    base_local = f"http://{host_anunciado}:{args.puerto_servidor}"
    confirm_api = f"{base_local}/api/transferCitizenConfirm"

    payload = {
        "id": cedula,
        "citizenName": f"Prueba Firma Transferencia {cedula}",
        "citizenEmail": f"carlos.prueba.firma.{cedula}@operador-origen-ejemplo.co",
        "contactEmail": "contacto.firma.transferencia@example.com",
        "urlDocuments": {
            "Documento firmado": f"{base_local}/firmado.pdf",
            "Documento alterado": f"{base_local}/alterado.pdf",
        },
        "documentsMetadata": [
            {"titulo": "Documento firmado", "tipo": "OTRO", "entidadEmisora": "Operador Origen (prueba)", "certificado": True},
            {"titulo": "Documento alterado", "tipo": "OTRO", "entidadEmisora": "Operador Origen (prueba)", "certificado": True},
        ],
        "confirmAPI": confirm_api,
    }

    try:
        print(f"\n1. POST {args.base_url}/api/transferCitizen (cedula {cedula})...")
        async with httpx.AsyncClient(timeout=15.0) as cliente:
            r = await cliente.post(f"{args.base_url}/api/transferCitizen", json=payload)
        print(f"   -> {r.status_code} {r.text}")
        if r.status_code not in (200, 201, 202):
            print("ABORTADO: la recepcion no acepto la transferencia.")
            sys.exit(1)

        async def _estado_activo() -> str | None:
            conn = await asyncpg.connect(args.database_url)
            try:
                fila = await conn.fetchrow("SELECT estado FROM ciudadano WHERE id = $1", cedula)
                return fila["estado"] if fila and fila["estado"] == "ACTIVO" else None
            finally:
                await conn.close()

        print("2. Esperando a que el ciudadano quede ACTIVO (la recepcion se resuelve en outbox)...")
        await _esperar("ciudadano ACTIVO tras la recepcion", _estado_activo)
        print(f"   -> confirmacion recibida en el operador de origen: {_Manejador.confirmacion}")

        async def _documentos() -> list[dict]:
            conn = await asyncpg.connect(args.database_url)
            try:
                filas = await conn.fetch(
                    "SELECT id, titulo, certificado, firma_valida, firma_firmante FROM documento "
                    "WHERE ciudadano_id = $1 ORDER BY titulo",
                    cedula,
                )
                return [dict(f) for f in filas]
            finally:
                await conn.close()

        print("3. Los dos documentos deben existir de inmediato (no hay cuarentena que los oculte)...")
        documentos = await _documentos()
        print(f"   -> {documentos}")
        if len(documentos) != 2:
            fallos.append(f"se esperaban 2 documentos, llegaron {len(documentos)}")

        async def _firmas_resueltas() -> bool:
            docs = await _documentos()
            return len(docs) == 2 and all(d["firma_valida"] is not None for d in docs)

        print("4. Esperando a que la bandeja de salida valide la firma de los dos (en segundo plano)...")
        await _esperar("firma_valida resuelta para los dos documentos", _firmas_resueltas)
        documentos = await _documentos()
        por_titulo = {d["titulo"]: d for d in documentos}
        print(f"   -> {documentos}")

        print("5. Verificando el documento firmado (certificado se mantiene)...")
        doc_firmado = por_titulo.get("Documento firmado")
        if doc_firmado is None:
            fallos.append("no se encontro 'Documento firmado'")
        else:
            if doc_firmado["firma_valida"] is not True:
                fallos.append(f"Documento firmado: se esperaba firma_valida=true, llego {doc_firmado['firma_valida']}")
            if doc_firmado["certificado"] is not True:
                fallos.append("Documento firmado: se esperaba que conservara certificado=true")
            if not doc_firmado["firma_firmante"] or FIRMANTE_CN not in doc_firmado["firma_firmante"]:
                fallos.append(f"Documento firmado: firmante inesperado {doc_firmado['firma_firmante']}")

        print("6. Verificando el documento alterado (pierde el certificado que declaro el origen)...")
        doc_alterado = por_titulo.get("Documento alterado")
        if doc_alterado is None:
            fallos.append("no se encontro 'Documento alterado'")
        else:
            if doc_alterado["firma_valida"] is not False:
                fallos.append(f"Documento alterado: se esperaba firma_valida=false, llego {doc_alterado['firma_valida']}")
            if doc_alterado["certificado"] is not False:
                fallos.append(
                    f"Documento alterado: se esperaba que perdiera el certificado (certificado=false), "
                    f"llego {doc_alterado['certificado']}"
                )

        print("7. Verificando la auditoria de la revocacion de certificacion...")
        conn = await asyncpg.connect(args.database_url)
        try:
            fila = await conn.fetchrow(
                "SELECT detalle FROM auditoria WHERE accion = 'documento.certificacion_revocada_por_firma_invalida' "
                "AND recurso = $1",
                str(doc_alterado["id"]) if doc_alterado else "",
            )
        finally:
            await conn.close()
        print(f"   -> {dict(fila) if fila else None}")
        if fila is None:
            fallos.append("se esperaba una auditoria 'documento.certificacion_revocada_por_firma_invalida' para el documento alterado")

        conn = await asyncpg.connect(args.database_url)
        try:
            fila_firmado = await conn.fetchrow(
                "SELECT 1 FROM auditoria WHERE accion = 'documento.certificacion_revocada_por_firma_invalida' "
                "AND recurso = $1",
                str(doc_firmado["id"]) if doc_firmado else "",
            )
        finally:
            await conn.close()
        if fila_firmado is not None:
            fallos.append("el documento firmado (valido) no deberia tener una revocacion de certificacion")

        print("8. Verificando que el ciudadano fue notificado por el centro de notificaciones (CU-17)...")
        conn = await asyncpg.connect(args.database_url)
        try:
            notificaciones = await conn.fetch(
                "SELECT asunto, cuerpo FROM notificacion WHERE ciudadano_id = $1 "
                "AND asunto = 'Un documento de tu carpeta dejó de estar certificado'",
                cedula,
            )
        finally:
            await conn.close()
        print(f"   -> {len(notificaciones)} notificacion(es) de revocacion: {[dict(n) for n in notificaciones]}")
        if len(notificaciones) != 1:
            fallos.append(
                f"se esperaba exactamente 1 notificacion de revocacion de certificado (una por el "
                f"documento alterado, ninguna por el firmado), llegaron {len(notificaciones)}"
            )
        elif "Documento alterado" not in notificaciones[0]["cuerpo"]:
            fallos.append("la notificacion deberia mencionar el titulo del documento afectado")

        print("9. Verificando el efecto en la cuota: el documento alterado ahora cuenta, el firmado no...")
        conn = await asyncpg.connect(args.database_url)
        try:
            fila_cuota = await conn.fetchrow(
                "SELECT COALESCE(SUM(tamano_bytes), 0) AS usado FROM documento "
                "WHERE ciudadano_id = $1 AND certificado = false AND estado = 'ACTIVO'",
                cedula,
            )
        finally:
            await conn.close()
        usado = fila_cuota["usado"]
        esperado = len(_Manejador.alterado)
        print(f"   -> usado_bytes (temporales activos) = {usado} (esperado {esperado}, solo el alterado)")
        if usado != esperado:
            fallos.append(
                f"tras la revocacion, se esperaba que solo el documento alterado ({esperado} bytes) "
                f"contara contra la cuota, llego usado_bytes={usado}"
            )

    finally:
        servidor.shutdown()

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print(
        "CU-09 enganchado en CU-16 (firmado conserva certificado, alterado lo pierde, "
        "notificación y efecto en cuota incluidos) probado sin fallos."
    )


if __name__ == "__main__":
    asyncio.run(main())
