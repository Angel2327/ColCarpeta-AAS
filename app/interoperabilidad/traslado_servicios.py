"""Capa de servicios de CU-03 (traslado a otro operador): la lógica de negocio de
`POST /api/v1/perfil/traslado` (`app.interoperabilidad.transferencias.router_propio`),
compartida con la pantalla de traslado del portal (`app.portal`). Ver AD-11
(docs/especificacion.md).

Vive en `app.interoperabilidad` (no en `app.identidad`) porque toca `OperadorCache` y
`Transferencia`, propios de este módulo -- pero sigue sin ser el módulo que habla con
el centralizador (`govcarpeta.py`): solo lee `operador_cache`, que otro proceso
(`app.interoperabilidad.outbox`) ya mantiene actualizado en segundo plano.
"""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import select

from app.config import get_config
from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.models import Auditoria, Ciudadano, EstadoCiudadano, EstadoTransferencia, OperadorCache, Outbox, Transferencia


def url_transferencia_utilizable(url: str | None, *, exigir_https: bool) -> bool:
    """Unica funcion que decide si un `transfer_api_url` del directorio sirve de
    verdad -- la usan por igual el desplegable del portal (`listar_operadores_
    transferibles`), la ruta que recibe la solicitud (`solicitar_traslado`) y el
    envio real en segundo plano (`app.interoperabilidad.outbox._enviar_transferencia`),
    para que nunca se desincronicen en tres copias del mismo chequeo.

    El directorio del MinTIC es dato sucio (CLAUDE.md, "trampa 5"): trae URLs sin TLS,
    con espacios, y algunas que ni siquiera apuntan a un host real
    (`http://0.0.0.0:8000`, dominios sueltos sin punto). Bien formada y con el esquema
    que exige la configuracion siempre; el chequeo de host "publico" (nada de
    localhost, 0.0.0.0, 127.x, o un host sin punto) solo se aplica cuando
    `exigir_https` esta prendido -- la misma bandera que ya marca "estamos contra el
    directorio real", nunca un entorno de prueba local. Con `exigir_https=False`
    (docker-compose.test.yml, instancias propias sin TLS) los nombres de servicio de
    Docker (`app-b`, sin punto) son exactamente lo que se espera, no dato sucio."""
    if not url:
        return False
    try:
        partes = urlparse(url.strip())
    except ValueError:
        return False
    esquemas_validos = {"https"} if exigir_https else {"http", "https"}
    if partes.scheme not in esquemas_validos:
        return False
    host = (partes.hostname or "").lower()
    if not host:
        return False
    if exigir_https and (host in ("localhost", "0.0.0.0") or host.startswith("127.") or "." not in host):
        return False
    return True


async def listar_operadores_transferibles() -> list[OperadorCache]:
    """Operadores del directorio que publican un endpoint de transferencia realmente
    utilizable -- de los ~73 operadores del directorio real, solo una fracción publica
    uno (CLAUDE.md, "trampa 5"), y de esos varios apuntan a direcciones que nunca
    responderían (`0.0.0.0`, dominios sin punto, URLs sin TLS). Mismo filtro que usa
    el envío real (`url_transferencia_utilizable`), nunca una copia -- así el
    desplegable nunca ofrece un destino que el envío rechazaría de entrada.

    Encima de ese filtro, descarta también los `_id` en `OPERADORES_EXCLUIDOS`
    (docs/especificacion.md, "Directorio de operadores"): una decisión de operación
    propia sobre qué le ofrecemos al ciudadano, nunca una corrección del directorio
    -- por `_id`, nunca por `nombre` (el directorio trae nombres duplicados).

    Ordenados por nombre para que la lista sea navegable."""
    cfg = get_config()
    async with SessionLocal() as session:
        resultado = await session.execute(select(OperadorCache).order_by(OperadorCache.nombre))
        operadores = list(resultado.scalars().all())

    excluidos = cfg.operadores_excluidos_ids
    return [
        o
        for o in operadores
        if o.id not in excluidos and url_transferencia_utilizable(o.transfer_api_url, exigir_https=cfg.transferencia_exigir_https)
    ]


async def solicitar_traslado(*, ciudadano_id: int, operador_destino_id: str, correlation_id: str | None) -> EstadoCiudadano:
    """Ver el docstring original de `POST /api/v1/perfil/traslado` para el detalle de
    cada rechazo. Devuelve el nuevo estado del ciudadano (`EN_TRANSFERENCIA`)."""
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, ciudadano_id)
        assert ciudadano is not None

        if ciudadano.estado != EstadoCiudadano.ACTIVO:
            raise ErrorDeNegocio("ESTADO_INVALIDO", "Tu carpeta no está activa.")

        ya_en_curso = (
            await session.execute(
                select(Transferencia.id).where(
                    Transferencia.ciudadano_id == ciudadano_id, Transferencia.estado == EstadoTransferencia.ENVIADA
                )
            )
        ).first()
        if ya_en_curso is not None:
            raise ErrorDeNegocio("TRASLADO_EN_CURSO", "Ya hay un traslado en curso para tu cédula.")

        operador = await session.get(OperadorCache, operador_destino_id)
        url_valida = operador is not None and url_transferencia_utilizable(
            operador.transfer_api_url, exigir_https=get_config().transferencia_exigir_https
        )
        if not url_valida:
            raise ErrorDeNegocio(
                "OPERADOR_NO_DISPONIBLE",
                "El operador destino no está en el directorio o no publica un endpoint de transferencia seguro.",
            )

        ciudadano.estado = EstadoCiudadano.EN_TRANSFERENCIA
        session.add(
            Outbox(
                operacion="enviarTransferencia",
                payload={
                    "cedula": ciudadano_id,
                    "operador_destino_id": operador_destino_id,
                    "correlation_id": correlation_id,
                },
            )
        )
        session.add(
            Auditoria(
                actor=str(ciudadano_id),
                accion="transferencia.solicitada",
                recurso=str(ciudadano_id),
                ciudadano_id=ciudadano_id,
                correlation_id=correlation_id,
                detalle={"operador_destino_id": operador_destino_id},
            )
        )
        await session.commit()

        return ciudadano.estado


async def estado_traslado(*, ciudadano_id: int) -> tuple[EstadoCiudadano, Transferencia | None, str | None]:
    """Para la pantalla de estado del portal: el estado actual del ciudadano, su
    transferencia más reciente (si alguna vez trasladó o intentó trasladar su carpeta)
    y el nombre del operador destino de esa transferencia (se resuelve aparte, en vez
    de con `Transferencia.operador_destino`, para no depender de una relación
    perezosa una vez que la sesión ya se cerró)."""
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, ciudadano_id)
        assert ciudadano is not None

        resultado = await session.execute(
            select(Transferencia)
            .where(Transferencia.ciudadano_id == ciudadano_id)
            .order_by(Transferencia.enviada_en.desc())
            .limit(1)
        )
        transferencia = resultado.scalar_one_or_none()
        nombre_operador = None
        if transferencia is not None:
            operador_destino = await session.get(OperadorCache, transferencia.operador_destino_id)
            nombre_operador = operador_destino.nombre if operador_destino is not None else transferencia.operador_destino_id

        estado = ciudadano.estado
    return estado, transferencia, nombre_operador
