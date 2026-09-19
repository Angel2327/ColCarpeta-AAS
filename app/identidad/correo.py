"""Generacion de la cuenta de correo de la carpeta (docs/especificacion.md, "Cuenta de
correo generada" y CU-01 paso 5).

Patron: nombre.apellido.anio@dominio, normalizado a minusculas y sin tildes ni caracteres
especiales; primer nombre y primer apellido tomados como la primera y la ultima palabra
del nombre completo. Ante colision se agrega un sufijo numerico incremental
(carlos.castro.2026.2@...).
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ciudadano

_NO_ALFANUMERICO = re.compile(r"[^a-z0-9]+")


def _normalizar(token: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", token).encode("ascii", "ignore").decode("ascii")
    return _NO_ALFANUMERICO.sub("", sin_tildes.lower())


def _primer_nombre_y_apellido(nombre_completo: str) -> tuple[str, str]:
    partes = [p for p in nombre_completo.split() if p]
    if not partes:
        raise ValueError("nombre vacio")
    primero = _normalizar(partes[0])
    ultimo = _normalizar(partes[-1]) if len(partes) > 1 else primero
    return primero, ultimo


async def _email_en_uso(session: AsyncSession, candidato: str) -> bool:
    resultado = await session.execute(select(Ciudadano.id).where(Ciudadano.email_carpeta == candidato))
    return resultado.scalar_one_or_none() is not None


async def generar_email_carpeta(session: AsyncSession, *, nombre_completo: str, anio: int, dominio: str) -> str:
    primero, ultimo = _primer_nombre_y_apellido(nombre_completo)
    base = f"{primero}.{ultimo}.{anio}"
    sufijo = 1
    while True:
        local = base if sufijo == 1 else f"{base}.{sufijo}"
        candidato = f"{local}@{dominio}"
        if not await _email_en_uso(session, candidato):
            return candidato
        sufijo += 1
