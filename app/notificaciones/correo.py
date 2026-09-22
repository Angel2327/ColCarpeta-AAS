"""Envio de notificaciones por correo.

Sin proveedor real integrado en esta entrega: el envio se simula, en el mismo sentido
en que otras dependencias externas del proyecto corren en modo simulado (la
Registraduria, o `TOTP_MODO=simulado`). No hay credenciales de SMTP ni de ningun
servicio de correo que configurar.

Quien llama a `enviar_correo` es responsable de dejar constancia en `auditoria` (esta
funcion no tiene acceso a la sesion de base de datos): la prueba de que el envio
ocurrio vive en la auditoria del caso de uso correspondiente, no aqui.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("colcarpeta.notificaciones")


async def enviar_correo(*, destinatario: str, asunto: str, cuerpo: str) -> None:
    """Simula el envio de un correo, registrandolo en el log del servidor."""
    logger.info("correo simulado -> %s | asunto: %s\n%s", destinatario, asunto, cuerpo)
