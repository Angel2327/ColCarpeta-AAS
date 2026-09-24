"""Cache-busting de los estaticos del portal: la URL de cada archivo bajo /static
lleva un parametro de version derivado de su propio contenido, no un numero escrito a
mano. Sin esto, `StaticFiles` solo responde con `ETag`/`Last-Modified` -- un navegador
que ya tenga el CSS en caché no vuelve a pedirlo tras un despliegue hasta que alguien
fuerza la recarga (encontrado en producción el 2026-09-24).

Con la URL cambiando sola en cuanto el archivo cambia, `app.main` puede servir estos
archivos con una cabecera `Cache-Control` agresiva (`immutable`) sin arriesgar nunca
servir una version vieja: una URL que ya se sirvio nunca cambia de contenido, y un
archivo que cambia siempre estrena URL.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs

DIRECTORIO_STATIC = Path(__file__).parent / "static"


@lru_cache(maxsize=None)
def _hash_archivo(nombre: str) -> str:
    """Los primeros 10 caracteres del SHA-256 del contenido alcanzan para evitar
    colisiones entre los pocos estaticos de este portal: no hace falta el hash
    completo en una URL que ya de por si es un parametro de caché, no un identificador
    de seguridad. Cacheado en memoria: el archivo no cambia mientras el proceso vive,
    un despliegue nuevo es un proceso nuevo."""
    contenido = (DIRECTORIO_STATIC / nombre).read_bytes()
    return hashlib.sha256(contenido).hexdigest()[:10]


def url_estatica(nombre: str) -> str:
    """URL versionada de un archivo bajo `/static` -- se usa desde las plantillas
    (global de Jinja `estatico`, registrado en `app.portal.router`) en vez de escribir
    `/static/...` a mano en cada `<link>`/`<script>`, para que ningún sitio se quede
    desactualizado si un despliegue cambia el archivo pero alguien olvida el número de
    versión."""
    return f"/static/{nombre}?v={_hash_archivo(nombre)}"


def cache_control_para(query_string: bytes) -> str:
    """Cache-Control según si la petición trae el parámetro de versión (`?v=...`, ver
    `url_estatica`): agresiva de verdad solo para una URL versionada, que nunca cambia
    de contenido. La URL sin versión -- todavía alcanzable por la redirección histórica
    `/portal/static/...` (`router_legado`, que apunta a `/static/...` sin versión a
    propósito, para no romper un enlace guardado con una versión que ya no existe) o
    por quien la escriba a mano -- no puede cachearse igual: esa sí puede cambiar de
    contenido en el próximo despliegue, y guardarla un año habría sido peor que el
    problema original, porque ya ni un despliegue nuevo la corrige (encontrado el
    2026-09-24, el mismo día que se agregó el cache-busting). Sin versión, `no-cache`
    obliga a revalidar contra el `ETag` que `StaticFiles` ya pone: sigue evitando
    volver a bajar el archivo si no cambió, solo ya no evita la ida y vuelta de red
    para preguntar."""
    if b"v" in parse_qs(query_string):
        return "public, max-age=31536000, immutable"
    return "no-cache"


@lru_cache(maxsize=1)
def manifest_versionado() -> bytes:
    """`site.webmanifest` declara sus propios iconos con una URL fija
    (`/static/icono-192.png`, etc.): al no ser una plantilla Jinja, no puede llamar a
    `url_estatica` por su cuenta. Se reescribe una sola vez aquí, leyendo el archivo
    real y agregándole la versión a cada ícono, para que el manifiesto tampoco sirva
    un ícono viejo desde la caché del navegador tras un despliegue."""
    datos = json.loads((DIRECTORIO_STATIC / "site.webmanifest").read_text(encoding="utf-8"))
    for icono in datos.get("icons", []):
        nombre = icono["src"].removeprefix("/static/")
        icono["src"] = url_estatica(nombre)
    return json.dumps(datos, ensure_ascii=False).encode("utf-8")
