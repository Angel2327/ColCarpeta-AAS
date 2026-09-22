"""CU-09: validación de la firma digital embebida en un PDF (PAdES), vía pyHanko.

Valida la integridad criptográfica de la firma: que el contenido no cambió después de
firmarse (`intact`), que la firma en sí es criptográficamente válida contra la llave
pública del certificado que dice haber firmado (`valid`), y que esa firma cubre el
archivo completo y no solo una parte (`coverage`). Extrae además quién dice ser el
firmante (el sujeto del certificado, tal como el certificado lo declara) y la fecha de
firma que el propio firmante reportó.

Si el PDF trae más de una firma, se valida y se reporta únicamente la última aplicada
(la más reciente, `embedded_signatures[-1]`): este proyecto no tiene un flujo de
cofirma, así que una sola firma por documento es lo único que se espera en la práctica.

Deliberadamente NO valida la cadena de confianza del certificado contra ninguna
autoridad certificadora. En Colombia eso exige el almacén de confianza de las
entidades acreditadas por la ONAC, que este proyecto no tiene -- afirmarlo sin tenerlo
sería simular una garantía que no existe. Por eso siempre se pasa un
`ValidationContext` sin raíces de confianza (`trust_roots=[]`, `allow_fetching=False`):
sin esto, pyHanko cae en su comportamiento por defecto (deprecado desde la versión
0.37.0) de validar contra el almacén de confianza del sistema operativo, que además de
ser el almacén equivocado para este caso, haría el resultado depender de en qué
máquina corre el proceso (Windows local vs. el contenedor Linux de Railway) y podría
intentar red (OCSP/CRL) para construirlo. `allow_fetching=False` further garantiza que
esta validación nunca sale a la red: es puramente local y determinista.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import async_validate_pdf_signature
from pyhanko.sign.validation.status import SignatureCoverageLevel
from pyhanko_certvalidator.context import ValidationContext

logger = logging.getLogger("colcarpeta.documentos.firma")

# Sin raices de confianza y sin permiso de red: ver docstring del modulo. Es un objeto
# inmutable y sin estado, seguro de compartir entre validaciones concurrentes.
_CONTEXTO_SIN_CADENA_DE_CONFIANZA = ValidationContext(trust_roots=[], allow_fetching=False)


@dataclass(frozen=True)
class ResultadoFirma:
    firma_valida: bool
    firmante: str | None
    fecha_firma: datetime | None


async def validar_firma_pdf(contenido: bytes) -> ResultadoFirma | None:
    """Valida la firma digital embebida en un PDF, si trae una.

    Es `async` porque pyHanko lo es a este nivel (la validacion de PDF hace su propio
    manejo de corutinas internamente); no se puede llamar desde una funcion sincrona
    que ya este dentro de un event loop (p. ej. el bucle de la bandeja de salida) sin
    usar esta variante -- la version sincrona de pyHanko hace su propio
    `asyncio.run(...)` por dentro, que revienta si ya hay un event loop corriendo.

    Devuelve `None` cuando no hay nada que validar: el archivo no es un PDF
    interpretable, o es un PDF sin ninguna firma embebida. En ambos casos
    `documento.firma_valida` debe quedar sin valor (`None`) -- eso es intencional y
    distinto de que la validación haya corrido y encontrado una firma inválida
    (`ResultadoFirma.firma_valida=False`), que sí es un resultado concreto.
    """
    try:
        lector = PdfFileReader(BytesIO(contenido))
        firmas = list(lector.embedded_signatures)
    except Exception:
        logger.warning("no se pudo interpretar el archivo como PDF valido; sin firma que validar", exc_info=True)
        return None

    if not firmas:
        return None

    firma = firmas[-1]
    estado = await async_validate_pdf_signature(firma, signer_validation_context=_CONTEXTO_SIN_CADENA_DE_CONFIANZA)

    cubre_todo_el_archivo = estado.coverage == SignatureCoverageLevel.ENTIRE_FILE
    firma_valida = bool(estado.intact and estado.valid and cubre_todo_el_archivo)

    return ResultadoFirma(
        firma_valida=firma_valida,
        firmante=estado.signing_cert.subject.human_friendly,
        fecha_firma=estado.signer_reported_dt,
    )
