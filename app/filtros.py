"""Normalización compartida de filtros de texto (búsqueda parcial, sin distinguir
mayúsculas ni acentos) usada por `app.documentos.servicios` y `app.admin.servicios` --
punto único para no repetir la misma lógica de recorte/escape/acentos en cada filtro.

Requiere la extensión `unaccent` de PostgreSQL (migración `6c5a3c89d46e`). Solo se usa
para los filtros de texto que una PERSONA escribe a mano y que otra persona pudo haber
escrito con o sin acento (tipo, entidad emisora, título de un documento, nombre de un
ciudadano) -- nunca para `cédula` (son dígitos, un acento no aplica ahí) ni para
`acción` de auditoría (vocabulario interno del código, siempre sin acentos, nunca un
valor que un ciudadano escriba)."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement

# Carácter de escape que se le pasa a ilike()/like() junto con el patrón: cualquier
# otro caracter serviría, pero la barra invertida es la convención de SQL estándar.
ESCAPE = "\\"


def normalizar_filtro_texto(valor: str | None) -> str | None:
    """Recorta espacios al inicio y al final; si el resultado queda vacío, lo trata
    como si el filtro no se hubiera enviado (`None`) en vez de buscar una cadena vacía
    -- sin esto, un campo con solo espacios filtraba y no devolvía nada."""
    if valor is None:
        return None
    valor = valor.strip()
    return valor or None


def patron_contiene(valor: str) -> str:
    """Arma el patrón `%valor%` para una búsqueda de coincidencia parcial, escapando
    `%` y `_` para que se busquen como caracteres literales -- sin esto, un valor con
    un `%` adentro (p. ej. un título "Descuento 50% aprobado") actuaría como comodín de
    SQL y ensancharía la búsqueda en vez de acotarla. Usar siempre junto con
    `escape=ESCAPE` en la llamada a `ilike()`/`like()`; `valor` ya debe venir de
    `normalizar_filtro_texto` (no vacío, sin espacios sobrantes)."""
    escapado = valor.replace(ESCAPE, ESCAPE * 2).replace("%", f"{ESCAPE}%").replace("_", f"{ESCAPE}_")
    return f"%{escapado}%"


def columna_sin_acento_contiene(columna: ColumnElement[str], valor: str) -> ColumnElement[bool]:
    """Condición de coincidencia parcial, sin distinguir mayúsculas NI acentos: envuelve
    ambos lados de la comparación en `unaccent(...)` (requiere la extensión de
    PostgreSQL instalada, ver la migración `6c5a3c89d46e`). `valor` ya debe venir de
    `normalizar_filtro_texto` (no vacío, sin espacios sobrantes); el escape de `%`/`_`
    se aplica aquí mismo, como en `patron_contiene`."""
    return func.unaccent(columna).ilike(func.unaccent(patron_contiene(valor)), escape=ESCAPE)
