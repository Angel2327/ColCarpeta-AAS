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

from sqlalchemy import select

from app.config import get_config
from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.models import Auditoria, Ciudadano, EstadoCiudadano, EstadoTransferencia, OperadorCache, Outbox, Transferencia


async def listar_operadores_transferibles() -> list[OperadorCache]:
    """Operadores del directorio que publican un endpoint de transferencia utilizable
    (con `transfer_api_url`, y `https://` si `TRANSFERENCIA_EXIGIR_HTTPS` lo exige) --
    de los ~73 operadores del directorio real, solo una fracción publica uno (CLAUDE.md,
    "trampa 5"). Ordenados por nombre para que la lista sea navegable."""
    cfg = get_config()
    async with SessionLocal() as session:
        resultado = await session.execute(
            select(OperadorCache).where(OperadorCache.transfer_api_url.is_not(None)).order_by(OperadorCache.nombre)
        )
        operadores = list(resultado.scalars().all())

    if cfg.transferencia_exigir_https:
        operadores = [o for o in operadores if o.transfer_api_url and o.transfer_api_url.startswith("https://")]
    return operadores


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
        url_valida = operador is not None and operador.transfer_api_url and (
            operador.transfer_api_url.startswith("https://") or not get_config().transferencia_exigir_https
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
