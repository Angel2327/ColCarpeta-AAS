"""Envio de notificaciones por correo, y su registro en el centro de notificaciones
(CU-17).

Sin proveedor real integrado en esta entrega: el envio se simula, en el mismo sentido
en que otras dependencias externas del proyecto corren en modo simulado (la
Registraduria, o `TOTP_MODO=simulado`). No hay credenciales de SMTP ni de ningun
servicio de correo que configurar.

Cada llamada a `enviar_correo` hace dos cosas a la vez: lo deja en el log del servidor (la
simulacion del envio) y crea la fila de `notificacion` que el ciudadano puede consultar
y marcar como leida por `GET/POST /api/v1/notificaciones`. Es el unico lugar del
proyecto que crea notificaciones -- no hay un mecanismo paralelo.

Quien llama a esta funcion sigue siendo responsable de dejar constancia en `auditoria`
del caso de uso correspondiente (esta funcion no audita nada por si sola: audita el
"por que" del envio, no el hecho generico de que se envio un correo).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notificacion

logger = logging.getLogger("colcarpeta.notificaciones")


async def enviar_correo(session: AsyncSession, *, ciudadano_id: int, destinatario: str, asunto: str, cuerpo: str) -> None:
    """Simula el envio de un correo y lo registra en la bandeja de notificaciones del
    ciudadano. No hace commit: queda en la misma transaccion que quien llama."""
    logger.info("correo simulado -> %s | asunto: %s\n%s", destinatario, asunto, cuerpo)
    session.add(Notificacion(ciudadano_id=ciudadano_id, asunto=asunto, cuerpo=cuerpo))
