"""Simula ser un operador de origen que envia una transferencia de ciudadano a
nuestras propias rutas de recepcion (CU-16), para probar sin depender de otro equipo
del curso.

    python scripts/probar_transferencia.py
    python scripts/probar_transferencia.py --base-url https://colcarpeta-aas-production.up.railway.app

Por defecto usa la cedula 1234567890 -- la misma que docs/especificacion.md y
CLAUDE.md ya usan como ejemplo de "ya afiliada a otro operador real" (sirve para
probar E1 de CU-01). Con ella, `validateCitizen` responde 200 (no disponible) y el
proceso de recepcion (app.interoperabilidad.outbox._recibir_transferencia) falla ahi
mismo, ANTES de llegar a `registerCitizen`. Eso permite probar de punta a punta la
parte real del flujo -- validar limites, descargar el documento, normalizar
metadatos, invocar validateCitizen, invocar confirmAPI con req_status=0 -- sin
registrar nada de verdad en el directorio del MinTIC.

CLAUDE.md prohibe llamar registerCitizen con cedulas inventadas "para probar": el
directorio es compartido por 72 equipos y esos registros quedan permanentes. Por eso
usar una cedula distinta a la 1234567890 exige el flag --confirmo-cedula-personalizada
a proposito, para que sea una decision explicita y no un accidente.

Ademas, antes de enviar nada, el script consulta `validateCitizen` de verdad para la
cedula elegida y aborta si responde 204 (disponible): que la 1234567890 este ocupada
hoy es un hecho de otro operador, no algo que este script controle -- si ese operador
la desliga alguna vez, seguir adelante a ciegas registraria una cedula inventada en el
directorio compartido. La seguridad es una comprobacion nuestra, no una coincidencia.

El script levanta un servidor HTTP minimo y local (solo libreria estandar) que hace de
"operador de origen" para recibir la llamada a `confirmAPI`, e imprime lo que llegue.
No depende de que el proceso de bandeja de salida corra en este mismo script: corre en
el servidor que atiende --base-url (normalmente tu `uvicorn --reload` de siempre).

--confirmacion-bind / --confirmacion-host existen para correr esto en dos contenedores
Docker separados en la misma red (ver docker-compose.test.yml): el servidor de
confirmacion debe escuchar en 0.0.0.0 dentro de su propio contenedor para que el OTRO
contenedor (el que corre la app) lo pueda alcanzar, pero la URL que se anuncia como
confirmAPI tiene que ser el nombre del servicio en esa red (no 0.0.0.0, que no es una
direccion a la que nadie mas se pueda conectar).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Permite ejecutar el script directamente sin depender de PYTHONPATH (mismo ajuste que
# scripts/limpiar_prueba.py y scripts/probar_govcarpeta.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.interoperabilidad import validar_ciudadano  # noqa: E402

CEDULA_SEGURA = 1234567890  # ya afiliada a otro operador real: nunca llega a registerCitizen
DOCUMENTO_DE_PRUEBA = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"


class _ManejadorConfirmacion(BaseHTTPRequestHandler):
    recibido: dict | None = None

    def do_POST(self) -> None:  # noqa: N802 -- nombre fijado por BaseHTTPRequestHandler
        largo = int(self.headers.get("Content-Length", 0))
        cuerpo = self.rfile.read(largo)
        try:
            _ManejadorConfirmacion.recibido = json.loads(cuerpo)
        except json.JSONDecodeError:
            _ManejadorConfirmacion.recibido = {"cuerpo_crudo": cuerpo.decode(errors="replace")}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"received": true}')

    def log_message(self, format: str, *args: object) -> None:  # silencia el log por defecto
        return


def _verificar_cedula_segura(cedula: int) -> None:
    """Comprobacion real contra el centralizador, no una suposicion sobre la cedula.

    204 = disponible = nadie la tiene afiliada: seguir adelante registraria de verdad
    una cedula inventada en el directorio compartido del MinTIC. Se aborta.
    200 = ya afiliada a otro operador: es seguro, `registerCitizen` nunca se alcanza.
    """
    print(f"Verificando validateCitizen para la cedula {cedula} antes de continuar...")
    try:
        resultado = asyncio.run(validar_ciudadano(cedula))
    except Exception as exc:
        print(
            f"ABORTADO: no se pudo consultar validateCitizen para {cedula}: {exc}\n"
            "Sin esa confirmacion no hay forma segura de continuar.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    if resultado.disponible:
        print(
            f"ABORTADO: validateCitizen dice que la cedula {cedula} esta DISPONIBLE (204,\n"
            "sin afiliar a nadie). Si este script continuara, ColCarpeta terminaria\n"
            "invocando registerCitizen DE VERDAD para esa cedula contra el directorio\n"
            "compartido del MinTIC -- CLAUDE.md lo prohibe cuando la cedula no es\n"
            "realmente tuya. Elige una cedula que sepas que ya esta afiliada a otro\n"
            "operador, o coordina con tu instructor una cedula de prueba legitima.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print(f"OK: {cedula} ya esta afiliada a otro operador ({resultado.mensaje}). Es seguro continuar.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="URL de ColCarpeta a probar")
    parser.add_argument("--cedula", type=int, default=CEDULA_SEGURA)
    parser.add_argument("--nombre", default="Carlos Prueba Transferencia")
    parser.add_argument("--puerto-confirmacion", type=int, default=9302)
    parser.add_argument(
        "--confirmacion-bind",
        default="127.0.0.1",
        help="interfaz donde escucha el servidor local de confirmacion (0.0.0.0 para que "
        "un contenedor Docker distinto lo pueda alcanzar; ver docker-compose.test.yml)",
    )
    parser.add_argument(
        "--confirmacion-host",
        default=None,
        help="host que se anuncia en confirmAPI si es distinto de --confirmacion-bind "
        "(obligatorio cuando --confirmacion-bind es 0.0.0.0): p. ej. el nombre de este "
        "servicio en la red de Docker Compose",
    )
    parser.add_argument(
        "--espera-segundos", type=int, default=90, help="cuanto esperar a que llegue la confirmacion"
    )
    parser.add_argument(
        "--confirmo-cedula-personalizada",
        action="store_true",
        help=(
            "obligatorio si --cedula no es la 1234567890 (CLAUDE.md: no inventar "
            "cedulas para probar registerCitizen)"
        ),
    )
    args = parser.parse_args()

    if args.cedula != CEDULA_SEGURA and not args.confirmo_cedula_personalizada:
        print(
            f"ADVERTENCIA: la cedula {args.cedula} no es la {CEDULA_SEGURA} (ya afiliada a otro\n"
            "operador real). Si validateCitizen la ve disponible, ColCarpeta invocara\n"
            "registerCitizen DE VERDAD contra el directorio compartido del MinTIC -- CLAUDE.md\n"
            "prohibe hacer eso con cedulas inventadas para probar. Repite el comando agregando\n"
            "--confirmo-cedula-personalizada si de verdad quieres continuar.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    _verificar_cedula_segura(args.cedula)

    host_anunciado = args.confirmacion_host or args.confirmacion_bind
    if host_anunciado == "0.0.0.0":
        print(
            "ERROR: --confirmacion-bind es 0.0.0.0 pero no se dio --confirmacion-host. "
            "0.0.0.0 no es una direccion a la que el otro operador (o contenedor) se "
            "pueda conectar; hace falta un host real para anunciar en confirmAPI.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    servidor = HTTPServer((args.confirmacion_bind, args.puerto_confirmacion), _ManejadorConfirmacion)
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    confirm_api = f"http://{host_anunciado}:{args.puerto_confirmacion}/api/transferCitizenConfirm"

    payload = {
        "id": args.cedula,
        "citizenName": args.nombre,
        # Vacio a proposito: ejercita "citizenEmail vacio o invalido" de "Tolerancia al
        # recibir" (se genera una direccion propia y queda constancia en auditoria).
        "citizenEmail": "",
        "contactEmail": "prueba.transferencia@example.com",
        "urlDocuments": {"Documento de prueba": DOCUMENTO_DE_PRUEBA},
        "documentsMetadata": [{"tipo": "OTRO", "entidadEmisora": "Operador de prueba", "certificado": False}],
        "confirmAPI": confirm_api,
    }

    print(f"Servidor de confirmacion local escuchando en {confirm_api}")
    print(f"\nPOST {args.base_url}/api/transferCitizen")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    try:
        respuesta = httpx.post(f"{args.base_url}/api/transferCitizen", json=payload, timeout=10.0)
    except httpx.HTTPError as exc:
        print(f"\nNo se pudo conectar a {args.base_url}: {exc}", file=sys.stderr)
        servidor.shutdown()
        raise SystemExit(1) from exc

    print(f"\nRespuesta inmediata: {respuesta.status_code} {respuesta.text}")
    print(
        "\nLa ruta solo encolo la recepcion en outbox (operacion='receiveTransferCitizen') y "
        "respondio rapido, tal como pide la especificacion. La descarga del documento, "
        "validateCitizen y la llamada a confirmAPI las hace el proceso de bandeja de salida "
        f"del servidor detras de {args.base_url} -- asegurate de que este corriendo con ese "
        "proceso activo (uvicorn app.main:app --reload lo arranca solo)."
    )
    print(f"\nEsperando hasta {args.espera_segundos} s la confirmacion...")

    inicio = time.monotonic()
    while time.monotonic() - inicio < args.espera_segundos:
        if _ManejadorConfirmacion.recibido is not None:
            print(f"\nConfirmacion recibida: {_ManejadorConfirmacion.recibido}")
            print(
                "\nCon la cedula segura, se espera req_status=0 (validateCitizen la encontro "
                "afiliada a otro operador antes de llegar a registerCitizen)."
            )
            break
        time.sleep(2)
    else:
        print(
            "\nNo llego ninguna confirmacion en el tiempo de espera. Revisa en la base de datos "
            "la tabla outbox (operacion IN ('receiveTransferCitizen', 'confirmarTransferencia')) "
            "y auditoria (recurso = la cedula) para ver en que quedo."
        )

    servidor.shutdown()


if __name__ == "__main__":
    main()
