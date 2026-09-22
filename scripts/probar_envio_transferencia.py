"""Simula el ciclo completo de CU-03 (envio) + CU-16 (recepcion) entre dos instancias
propias de ColCarpeta en Linux (docker-compose.test.yml: app-a envia, app-b recibe),
contra un centralizador falso (scripts/mock_centralizador.py) -- nunca contra el MinTIC
real ni el directorio compartido por los demas equipos del curso.

Uso (dentro de docker-compose.test.yml, ver ese archivo -- servicio "prueba-envio"):
    python scripts/probar_envio_transferencia.py \
        --base-url-a http://app-a:8000 \
        --database-url-a postgresql://postgres:postgres@postgres-a:5432/colcarpeta_a \
        --database-url-b postgresql://postgres:postgres@postgres-b:5432/colcarpeta_b \
        --operador-destino-id test-operador-b

Flujo:
  1. Registra un ciudadano de prueba en app-a (CU-01, contra el centralizador falso).
     La cedula termina en 1 a proposito: la Registraduria simulada solo confirma la
     identidad de inmediato para cedulas que no terminen en 0 (E2) ni en 9 (E3, demora
     y 504) -- ver CLAUDE.md, "Como probar los caminos de excepcion".
  2. Espera a que quede ACTIVO (la bandeja de salida de app-a procesa registerCitizen
     contra el centralizador falso).
  3. Inicia sesion y sube un documento (CU-05).
  4. Solicita el traslado a app-b (CU-03: POST /api/v1/perfil/traslado).
  5. Espera a que app-a resuelva la transferencia (CONFIRMADA, o recuperada si algo
     fallo) consultando directamente la base de datos de app-a -- no existe todavia un
     GET /api/v1/perfil que exponga el estado por API (ver CLAUDE.md, Pendiente).
  6. Si quedo CONFIRMADA, verifica en la base de datos de app-b que el ciudadano y su
     documento llegaron, y que app-a quedo TRASLADADO.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
import httpx  # noqa: E402

CEDULA_PRUEBA = 900100201  # ficticia: esta prueba nunca toca el MinTIC real (mock_centralizador.py)

# CU-05 solo admite application/pdf, image/jpeg o image/png, detectado por los primeros
# bytes reales del archivo (app/documentos/tipos.py), no por el nombre -- un PDF minimo
# valido es el mas simple de generar a mano.
PDF_DE_PRUEBA = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 3 3]>>endobj\n"
    b"trailer<</Size 4/Root 1 0 R>>\n"
    b"%%EOF\n"
)


async def _esperar(descripcion: str, condicion, intentos: int = 60, espera_segundos: float = 2.0):
    """Sondea `condicion` (coroutine sin argumentos) hasta que devuelva un valor
    truthy, o levanta TimeoutError tras `intentos` intentos."""
    for _ in range(intentos):
        valor = await condicion()
        if valor:
            return valor
        await asyncio.sleep(espera_segundos)
    raise TimeoutError(f"tiempo agotado esperando: {descripcion}")


async def _estado_ciudadano(dsn: str, cedula: int) -> str | None:
    conn = await asyncpg.connect(dsn)
    try:
        fila = await conn.fetchrow("SELECT estado FROM ciudadano WHERE id = $1", cedula)
        return fila["estado"] if fila else None
    finally:
        await conn.close()


async def _documentos(dsn: str, cedula: int) -> list[dict]:
    conn = await asyncpg.connect(dsn)
    try:
        filas = await conn.fetch("SELECT titulo, tamano_bytes FROM documento WHERE ciudadano_id = $1", cedula)
        return [dict(f) for f in filas]
    finally:
        await conn.close()


async def _transferencia(dsn: str, cedula: int) -> dict | None:
    conn = await asyncpg.connect(dsn)
    try:
        fila = await conn.fetchrow(
            "SELECT estado, purgar_despues_de FROM transferencia WHERE ciudadano_id = $1 ORDER BY id DESC LIMIT 1",
            cedula,
        )
        return dict(fila) if fila else None
    finally:
        await conn.close()


async def _solo_si(coro, valor_esperado):
    resultado = await coro
    return resultado if resultado == valor_esperado else None


async def _transferencia_resuelta(dsn: str, cedula: int):
    t = await _transferencia(dsn, cedula)
    if t is None:
        return None
    return t if t["estado"] != "ENVIADA" else None


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url-a", required=True)
    parser.add_argument("--database-url-a", required=True)
    parser.add_argument("--database-url-b", required=True)
    parser.add_argument("--operador-destino-id", required=True)
    parser.add_argument("--cedula", type=int, default=CEDULA_PRUEBA)
    args = parser.parse_args()

    cedula = args.cedula
    fallos: list[str] = []
    password = "ClaveDePrueba123!"

    async with httpx.AsyncClient(base_url=args.base_url_a, timeout=15.0) as cliente:
        print(f"1. Registrando cedula {cedula} en app-a...")
        r = await cliente.post(
            "/api/v1/registro",
            json={
                "cedula": cedula,
                # Incluye la cedula: dos corridas con el mismo nombre generarian el
                # mismo email_carpeta (patron habitual, por nombre+anio) y, si el
                # anterior ya se traslado a app-b y sigue existiendo alli (la purga
                # de app-a no toca lo que ya recibio otro operador), chocarian contra
                # la unicidad de email_carpeta en app-b -- un caso real y no manejado
                # hoy en la recepcion (ver CLAUDE.md), pero no lo que esta prueba
                # quiere ejercitar por defecto.
                "nombre": f"Prueba Envio Transferencia {cedula}",
                "direccion": "Calle de prueba 123",
                "email_personal": "prueba.envio@example.com",
                "telefono": "+573000000000",
                "password": password,
            },
        )
        print(f"   -> {r.status_code} {r.text}")
        if r.status_code != 201:
            print("ABORTADO: el registro no respondio 201.")
            sys.exit(1)

        print("2. Esperando a que quede ACTIVO (bandeja de salida de app-a)...")
        estado = await _esperar(
            "ciudadano ACTIVO en app-a",
            lambda: _solo_si(_estado_ciudadano(args.database_url_a, cedula), "ACTIVO"),
        )
        print(f"   -> estado={estado}")

        print("3. Iniciando sesion...")
        r = await cliente.post("/api/v1/sesion", json={"usuario": str(cedula), "password": password})
        print(f"   -> {r.status_code}")
        if r.status_code != 200:
            print(f"ABORTADO: no se pudo iniciar sesion: {r.text}")
            sys.exit(1)
        token = r.json()["access_token"]
        cliente.headers["Authorization"] = f"Bearer {token}"

        print("4. Subiendo un documento de prueba...")
        r = await cliente.post(
            "/api/v1/documentos",
            data={"titulo": "Documento de prueba de envio", "tipo": "OTRO"},
            files={"archivo": ("prueba.pdf", io.BytesIO(PDF_DE_PRUEBA), "application/pdf")},
        )
        print(f"   -> {r.status_code} {r.text}")
        if r.status_code not in (200, 201):
            print("ABORTADO: no se pudo cargar el documento.")
            sys.exit(1)

        print(f"5. Solicitando traslado a {args.operador_destino_id}...")
        r = await cliente.post("/api/v1/perfil/traslado", json={"operador_destino_id": args.operador_destino_id})
        print(f"   -> {r.status_code} {r.text}")
        if r.status_code != 202:
            print("ABORTADO: la solicitud de traslado no respondio 202.")
            sys.exit(1)

    print("6. Esperando la resolucion de la transferencia en app-a (CONFIRMADA, o recuperada si algo fallo)...")
    try:
        transferencia = await _esperar(
            "transferencia resuelta en app-a",
            lambda: _transferencia_resuelta(args.database_url_a, cedula),
        )
        print(f"   -> transferencia: {transferencia}")
    except TimeoutError as exc:
        fallos.append(str(exc))
        transferencia = None

    if transferencia and transferencia["estado"] == "CONFIRMADA":
        print("7. Verificando en app-b que el ciudadano y su documento llegaron...")
        try:
            estado_b = await _esperar(
                "ciudadano ACTIVO en app-b",
                lambda: _solo_si(_estado_ciudadano(args.database_url_b, cedula), "ACTIVO"),
            )
            print(f"   -> estado en app-b: {estado_b}")
        except TimeoutError as exc:
            fallos.append(str(exc))

        documentos_b = await _documentos(args.database_url_b, cedula)
        print(f"   -> documentos en app-b: {documentos_b}")
        if not documentos_b:
            fallos.append("app-b no tiene ningun documento para la cedula transferida")

        estado_a = await _estado_ciudadano(args.database_url_a, cedula)
        print(f"   -> estado final en app-a: {estado_a}")
        if estado_a != "TRASLADADO":
            fallos.append(f"app-a: se esperaba TRASLADADO, quedo en {estado_a}")
    elif transferencia:
        print(f"La transferencia NO quedo CONFIRMADA (estado={transferencia['estado']}); revisando recuperacion...")
        estado_a = await _estado_ciudadano(args.database_url_a, cedula)
        print(f"   -> estado del ciudadano en app-a tras la recuperacion: {estado_a}")
        fallos.append(f"la transferencia no se confirmo (estado={transferencia['estado']})")

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print("Prueba completada sin fallos detectados.")


if __name__ == "__main__":
    asyncio.run(main())
