"""Comunicacion con otros operadores del ecosistema (CU-16, transferencia de
ciudadanos). Junto con `govcarpeta.py`, es el unico lugar del proyecto que abre
conexiones salientes fuera de ColCarpeta -- CLAUDE.md: "app/interoperabilidad/ es el
unico modulo que habla con el centralizador y con otros operadores".

A diferencia del centralizador no hay un contrato unico ni endpoint fijo: cada URL
pertenece al operador que envio o recibe la transferencia (su bucket, su
`confirmAPI`). Tampoco hay autenticacion en el ecosistema (CLAUDE.md, trampa 6), asi
que estas llamadas son deliberadamente tolerantes, igual que pide "Tolerancia al
recibir" en docs/especificacion.md.

El cliente HTTP se crea una sola vez y se reutiliza (mismo patron que `GovCarpeta`),
con timeout explicito de httpx (connect/read/write/pool) en cada llamada y un limite
duro adicional por fuera con `asyncio.wait_for`.

Nota sobre un cuelgue observado en Windows (hipotesis, no conclusion): durante las
pruebas de CU-16 en esta maquina, el worker de outbox se colgo repetidas veces
haciendo HEAD/GET hacia otro operador, incluso con el limite duro de abajo. La pista
mas fuerte es una limitacion conocida de `ProactorEventLoop` (cancelar una operacion
de socket superpuesta puede quedar esperando indefinidamente a que la cancelacion
termine, asi que ni el timeout de httpx ni `asyncio.wait_for` alcanzan a cortar). Pero
no se descarto una causa distinta: durante buena parte de esas pruebas habia dos
procesos de uvicorn compitiendo por las mismas filas de outbox (ver CLAUDE.md), lo que
pudo enmascarar o exagerar el sintoma. No se reprodujo en ejecucion aislada (sin un
servidor HTTP vivo en el mismo event loop). Si esto es real y especifico de Windows,
no deberia afectar el despliegue en Railway (Linux); de todas formas conviene
confirmarlo antes de darlo por sentado (ver docs/wsl_docker.md).
"""

from __future__ import annotations

import asyncio

import httpx

TIMEOUT_DESCARGA = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
TIMEOUT_CONFIRMACION = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
# Limite duro adicional, forzado con asyncio.wait_for: un poco mas largo que
# TIMEOUT_DESCARGA.read para no pisarle el timeout normal de httpx.
LIMITE_DURO_SEGUNDOS = 75.0
# Tamano de trozo al leer en streaming (paso 2, limite de bytes).
TAMANO_TROZO_BYTES = 65_536

_cliente: httpx.AsyncClient | None = None


class OperadorNoDisponible(Exception):
    """El operador de origen o destino no respondio: tiempo agotado o error de red."""


def _obtener_cliente() -> httpx.AsyncClient:
    global _cliente
    if _cliente is None or _cliente.is_closed:
        # follow_redirects=True sirve para las tres cosas que este cliente hace (HEAD,
        # bajar documentos y notificar confirmAPI); el timeout se pasa por-llamada
        # porque descarga y confirmacion tienen presupuestos distintos.
        _cliente = httpx.AsyncClient(follow_redirects=True)
    return _cliente


async def cerrar() -> None:
    """Se llama junto con `GovCarpeta.cerrar()` al apagar el proceso de bandeja de
    salida (ver `ejecutar_bandeja_de_salida`)."""
    global _cliente
    if _cliente is not None:
        await _cliente.aclose()
        _cliente = None


async def _con_limite_duro(corutina, *, descripcion: str):
    """Envuelve una llamada del cliente compartido con un limite de tiempo forzado por
    fuera de httpx. Si se agota, se asume que el cliente pudo quedar en mal estado por
    la cancelacion y se descarta, para que la proxima llamada arranque con uno nuevo."""
    try:
        return await asyncio.wait_for(corutina, timeout=LIMITE_DURO_SEGUNDOS)
    except TimeoutError as exc:
        await cerrar()
        raise OperadorNoDisponible(f"{descripcion}: tiempo de espera agotado") from exc
    except httpx.HTTPError as exc:
        raise OperadorNoDisponible(f"{descripcion}: {exc}") from exc


async def obtener_tamano(url: str) -> int | None:
    """HEAD a la URL del documento. None si el operador no informa el tamano. Es solo
    una estimacion para el rechazo rapido del paso 1 ("Orden de recepcion"): la URL es
    de un tercero, no confiable, asi que `descargar` no se apoya en este valor para
    hacer cumplir el limite de verdad -- ver su propio docstring."""
    r = await _con_limite_duro(_obtener_cliente().head(url, timeout=TIMEOUT_DESCARGA), descripcion=f"HEAD {url}")
    if r.status_code >= 400:
        raise OperadorNoDisponible(f"HEAD {url}: respuesta {r.status_code}")
    largo = r.headers.get("content-length")
    return int(largo) if largo is not None and largo.isdigit() else None


async def descargar(url: str, *, limite_bytes: int) -> tuple[bytes, str | None]:
    """GET a la URL del documento del operador de origen, en streaming.

    No se confia en el `Content-Length` que declare el operador de origen (puede ser
    falso, o no venir): se descarga por trozos y se aborta apenas los bytes
    acumulados superan `limite_bytes`, sin esperar a terminar de bajar el cuerpo.
    Devuelve (contenido, content-type declarado por el remoto; puede ser None).
    """
    trozos: list[bytes] = []
    total = 0

    async def _transmitir() -> str | None:
        nonlocal total
        cliente = _obtener_cliente()
        async with cliente.stream("GET", url, timeout=TIMEOUT_DESCARGA) as respuesta:
            if respuesta.status_code >= 400:
                raise OperadorNoDisponible(f"GET {url}: respuesta {respuesta.status_code}")
            async for trozo in respuesta.aiter_bytes(TAMANO_TROZO_BYTES):
                total += len(trozo)
                if total > limite_bytes:
                    raise OperadorNoDisponible(
                        f"GET {url}: supero el limite de {limite_bytes} bytes durante la descarga"
                    )
                trozos.append(trozo)
            return respuesta.headers.get("content-type")

    try:
        content_type = await asyncio.wait_for(_transmitir(), timeout=LIMITE_DURO_SEGUNDOS)
    except TimeoutError as exc:
        await cerrar()
        raise OperadorNoDisponible(f"GET {url}: tiempo de espera agotado") from exc
    except httpx.HTTPError as exc:
        raise OperadorNoDisponible(f"GET {url}: {exc}") from exc

    return b"".join(trozos), content_type


async def confirmar_transferencia(*, url: str, cedula: int, req_status: int) -> None:
    """POST {id, req_status} al `confirmAPI` que trajo la transferencia entrante, o al
    que registramos nosotros para una transferencia saliente."""
    r = await _con_limite_duro(
        _obtener_cliente().post(url, json={"id": cedula, "req_status": req_status}, timeout=TIMEOUT_CONFIRMACION),
        descripcion=f"POST {url}",
    )
    if r.status_code >= 400:
        raise OperadorNoDisponible(f"POST {url}: respuesta {r.status_code}")
