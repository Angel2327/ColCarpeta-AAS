"""Deteccion de tipo de archivo por contenido (docs/especificacion.md, "Parametros y
limites" > Documentos: "Validados por contenido, no solo por extension").

Solo se admiten los tres tipos de la tabla de limites, asi que basta con reconocer sus
firmas binarias (magic numbers) sin depender de una libreria externa como libmagic.
"""

from __future__ import annotations

TIPOS_PERMITIDOS = ("application/pdf", "image/jpeg", "image/png")

_FIRMAS: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


def detectar_content_type(contenido: bytes) -> str | None:
    for firma, tipo in _FIRMAS:
        if contenido.startswith(firma):
            return tipo
    return None
