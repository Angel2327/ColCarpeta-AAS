"""Proceso de bandeja de salida (outbox).

Ver docs/especificacion.md, seccion "Reglas de operacion". `validateCitizen` es la unica
llamada sincrona dentro de una peticion del ciudadano; todo lo demas que toca al
centralizador se escribe en `outbox` dentro de la misma transaccion de negocio y lo
ejecuta este proceso en segundo plano.

Parametros (seccion "Parametros y limites"):
  - Intervalo del proceso: OUTBOX_INTERVALO_SEGUNDOS (por defecto 10 s).
  - Reintentos: 5, con espera exponencial de 1, 2, 4, 8 y 16 minutos.
  - Agotados los reintentos, la entrada queda en FALLIDO.

Se reintenta ante 500, 501, tiempo de espera agotado y error de red -- es decir, ante
`CentralizadorNoDisponible`. Cualquier otro error (por ejemplo el 501 "ya registrado" de
`registerCitizen`, que `GovCarpeta` traduce a `ValueError`) es de negocio y no se
reintenta: la entrada pasa a FALLIDO de una vez.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.interoperabilidad.govcarpeta import CentralizadorNoDisponible, GovCarpeta
from app.models import Auditoria, Ciudadano, EstadoCiudadano, EstadoOutbox, Outbox

logger = logging.getLogger("colcarpeta.outbox")

ESPERA_REINTENTOS_MINUTOS: tuple[int, ...] = (1, 2, 4, 8, 16)
REINTENTOS_MAXIMOS = len(ESPERA_REINTENTOS_MINUTOS)
TAMANO_LOTE = 20

Manejador = Callable[[GovCarpeta, dict], Awaitable[None]]


async def _registrar_ciudadano(gov: GovCarpeta, payload: dict) -> None:
    await gov.registrar_ciudadano(
        cedula=payload["cedula"],
        nombre=payload["nombre"],
        direccion=payload["direccion"],
        email=payload["email"],
    )


async def _desligar_ciudadano(gov: GovCarpeta, payload: dict) -> None:
    await gov.desligar_ciudadano(cedula=payload["cedula"])


async def _autenticar_documento(gov: GovCarpeta, payload: dict) -> None:
    await gov.autenticar_documento(
        cedula=payload["cedula"],
        url_documento=payload["url_documento"],
        titulo=payload["titulo"],
    )


MANEJADORES: dict[str, Manejador] = {
    "registerCitizen": _registrar_ciudadano,
    "unregisterCitizen": _desligar_ciudadano,
    "authenticateDocument": _autenticar_documento,
}


async def _activar_ciudadano(session: AsyncSession, payload: dict) -> None:
    """CU-01, paso 7: con 201 de registerCitizen el ciudadano pasa de PENDIENTE_CENTRALIZADOR a ACTIVO."""
    ciudadano = await session.get(Ciudadano, payload["cedula"])
    if ciudadano is not None and ciudadano.estado == EstadoCiudadano.PENDIENTE_CENTRALIZADOR:
        ciudadano.estado = EstadoCiudadano.ACTIVO


EfectoAlCompletar = Callable[[AsyncSession, dict], Awaitable[None]]

EFECTOS_AL_COMPLETAR: dict[str, EfectoAlCompletar] = {
    "registerCitizen": _activar_ciudadano,
}


async def _reclamar_uno(session: AsyncSession) -> Outbox | None:
    """Toma la entrada PENDIENTE mas antigua que ya puede intentarse y la marca EN_PROCESO.

    `FOR UPDATE SKIP LOCKED` es lo que permite correr varias replicas del proceso sin que
    dos instancias tomen la misma entrada.
    """
    ahora = datetime.now(timezone.utc)
    resultado = await session.execute(
        select(Outbox)
        .where(Outbox.estado == EstadoOutbox.PENDIENTE)
        .where(or_(Outbox.proximo_intento.is_(None), Outbox.proximo_intento <= ahora))
        .order_by(Outbox.creado_en)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    entrada = resultado.scalar_one_or_none()
    if entrada is not None:
        entrada.estado = EstadoOutbox.EN_PROCESO
    return entrada


def _identificador_recurso(payload: dict) -> str | None:
    valor = payload.get("cedula")
    return str(valor) if valor is not None else None


async def _ejecutar(gov: GovCarpeta, operacion: str, payload: dict) -> tuple[str | None, bool]:
    """Ejecuta la operacion. Devuelve (error, reintentable); error=None si tuvo exito."""
    manejador = MANEJADORES.get(operacion)
    if manejador is None:
        return f"operacion desconocida: {operacion}", False
    try:
        await manejador(gov, payload)
    except CentralizadorNoDisponible as exc:
        return str(exc)[:2000], True
    except Exception as exc:  # error de negocio (p. ej. ya registrado): no se reintenta
        return str(exc)[:2000], False
    return None, False


def _finalizar(entrada: Outbox, intentos_previos: int, error: str | None, reintentable: bool) -> None:
    entrada.intentos = intentos_previos + 1
    if error is None:
        entrada.estado = EstadoOutbox.COMPLETADO
        entrada.ultimo_error = None
        return

    entrada.ultimo_error = error
    if reintentable and entrada.intentos <= REINTENTOS_MAXIMOS:
        espera = timedelta(minutes=ESPERA_REINTENTOS_MINUTOS[entrada.intentos - 1])
        entrada.estado = EstadoOutbox.PENDIENTE
        entrada.proximo_intento = datetime.now(timezone.utc) + espera
    else:
        entrada.estado = EstadoOutbox.FALLIDO


async def procesar_lote(
    session_factory: async_sessionmaker[AsyncSession], gov: GovCarpeta, limite: int = TAMANO_LOTE
) -> int:
    """Procesa hasta `limite` entradas listas para intentarse. Devuelve cuantas tramito.

    Cada entrada se reclama y se finaliza en transacciones cortas separadas: la llamada de
    red (hasta 30 s de lectura) queda fuera de la transaccion para no retener el bloqueo
    de fila mientras se espera al centralizador.
    """
    procesadas = 0
    for _ in range(limite):
        async with session_factory() as session, session.begin():
            entrada = await _reclamar_uno(session)
            if entrada is None:
                return procesadas
            entrada_id = entrada.id
            operacion = entrada.operacion
            payload = dict(entrada.payload)
            intentos_previos = entrada.intentos

        error, reintentable = await _ejecutar(gov, operacion, payload)

        async with session_factory() as session, session.begin():
            entrada = await session.get(Outbox, entrada_id, with_for_update=True)
            assert entrada is not None
            _finalizar(entrada, intentos_previos, error, reintentable)
            if entrada.estado == EstadoOutbox.COMPLETADO:
                efecto = EFECTOS_AL_COMPLETAR.get(operacion)
                if efecto is not None:
                    await efecto(session, payload)
            if entrada.estado in (EstadoOutbox.COMPLETADO, EstadoOutbox.FALLIDO):
                session.add(
                    Auditoria(
                        actor="sistema",
                        accion=f"outbox.{operacion}",
                        recurso=_identificador_recurso(payload),
                        ciudadano_id=payload.get("cedula"),
                        correlation_id=payload.get("correlation_id"),
                        detalle={
                            "outbox_id": entrada_id,
                            "estado": entrada.estado.value,
                            "intentos": entrada.intentos,
                            "error": error,
                        },
                    )
                )

        procesadas += 1
        if error is not None:
            logger.warning("outbox %s (%s) intento %s fallo: %s", entrada_id, operacion, intentos_previos + 1, error)

    return procesadas


async def ejecutar_bandeja_de_salida(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    intervalo_segundos: float | None = None,
) -> None:
    """Bucle de fondo: procesa lotes de `outbox` cada `intervalo_segundos`.

    Pensado para lanzarse como una `asyncio.Task` desde el ciclo de vida de la aplicacion
    y cancelarse al apagar.
    """
    from app.config import get_config
    from app.db import SessionLocal

    factory = session_factory or SessionLocal
    intervalo = intervalo_segundos if intervalo_segundos is not None else get_config().outbox_intervalo_segundos
    gov = GovCarpeta()
    try:
        while True:
            try:
                await procesar_lote(factory, gov)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("fallo inesperado procesando la bandeja de salida")
            await asyncio.sleep(intervalo)
    finally:
        await gov.cerrar()
