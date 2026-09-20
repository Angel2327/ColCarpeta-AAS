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
registerCitizen, o el 204 "no autenticado" de authenticateDocument, que `GovCarpeta`
traduce a `ValueError`) es de negocio y no se reintenta: la entrada pasa a FALLIDO de
una vez.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.interoperabilidad.govcarpeta import CentralizadorNoDisponible, GovCarpeta
from app.models import (
    Auditoria,
    Ciudadano,
    Documento,
    EstadoAutenticacionDocumento,
    EstadoCiudadano,
    EstadoOutbox,
    Outbox,
)

logger = logging.getLogger("colcarpeta.outbox")

ESPERA_REINTENTOS_MINUTOS: tuple[int, ...] = (1, 2, 4, 8, 16)
REINTENTOS_MAXIMOS = len(ESPERA_REINTENTOS_MINUTOS)
TAMANO_LOTE = 20

Manejador = Callable[[GovCarpeta, dict], Awaitable[Any]]


class DocumentoEliminado(Exception):
    """CU-11, E5: el documento se elimino entre la solicitud y el envio.

    No hay un estado CANCELADO en `outbox.estado` (ni consola de administracion que lo
    distinga de FALLIDO todavia), asi que se trata como un fallo de negocio: no se
    reintenta, la entrada pasa a FALLIDO y el efecto de finalizacion no encuentra
    documento que actualizar.
    """


async def _registrar_ciudadano(gov: GovCarpeta, payload: dict) -> None:
    await gov.registrar_ciudadano(
        cedula=payload["cedula"],
        nombre=payload["nombre"],
        direccion=payload["direccion"],
        email=payload["email"],
    )


async def _desligar_ciudadano(gov: GovCarpeta, payload: dict) -> None:
    await gov.desligar_ciudadano(cedula=payload["cedula"])


async def _autenticar_documento(gov: GovCarpeta, payload: dict) -> str:
    # Import diferido: evita el ciclo interoperabilidad <-> documentos (este ultimo ya
    # importa cosas de identidad/interoperabilidad indirectamente via el resto de la app).
    from app.config import get_config
    from app.db import SessionLocal
    from app.documentos.almacenamiento import generar_url_descarga

    documento_id = uuid.UUID(payload["documento_id"])
    async with SessionLocal() as session:
        documento = await session.get(Documento, documento_id)
        if documento is None:
            raise DocumentoEliminado(f"el documento {documento_id} ya no existe")
        s3_key = documento.s3_key
        ciudadano_id = documento.ciudadano_id

    # E4: el enlace se genera de nuevo en cada intento (incluidos los reintentos), asi
    # que siempre esta fresco en el momento exacto de la llamada al centralizador --
    # nunca puede vencer "antes de la descarga" porque no se reutiliza uno viejo.
    cfg = get_config()
    url = generar_url_descarga(clave=s3_key, ttl_segundos=cfg.presigned_url_ttl_auth)

    async with SessionLocal() as session:
        # "Cada generacion de un enlace firmado se registra en auditoria con el
        # documento, el destino y el momento" (Seguridad y manejo de documentos).
        session.add(
            Auditoria(
                actor="sistema",
                accion="documento.enlace_generado",
                recurso=payload["documento_id"],
                ciudadano_id=ciudadano_id,
                correlation_id=payload.get("correlation_id"),
                detalle={"destino": "centralizador"},
            )
        )
        await session.commit()

    return await gov.autenticar_documento(cedula=payload["cedula"], url_documento=url, titulo=payload["titulo"])


MANEJADORES: dict[str, Manejador] = {
    "registerCitizen": _registrar_ciudadano,
    "unregisterCitizen": _desligar_ciudadano,
    "authenticateDocument": _autenticar_documento,
}


class ResultadoOperacion(str, enum.Enum):
    EXITO = "EXITO"
    RECHAZO = "RECHAZO"  # fallo de negocio, no reintentable (p. ej. 204, 501 ya registrado)
    REINTENTOS_AGOTADOS = "REINTENTOS_AGOTADOS"  # fallo transitorio, se acabaron los intentos


async def _activar_ciudadano(
    session: AsyncSession, payload: dict, resultado: ResultadoOperacion, respuesta: Any, error: str | None
) -> None:
    """CU-01, paso 7: con 201 de registerCitizen el ciudadano pasa de PENDIENTE_CENTRALIZADOR a ACTIVO.

    En fallo (RECHAZO o REINTENTOS_AGOTADOS) no se hace nada: E5/E6 de CU-01 dicen
    explicitamente que el ciudadano permanece en PENDIENTE_CENTRALIZADOR.
    """
    if resultado != ResultadoOperacion.EXITO:
        return
    ciudadano = await session.get(Ciudadano, payload["cedula"])
    if ciudadano is not None and ciudadano.estado == EstadoCiudadano.PENDIENTE_CENTRALIZADOR:
        ciudadano.estado = EstadoCiudadano.ACTIVO


async def _actualizar_autenticacion_documento(
    session: AsyncSession, payload: dict, resultado: ResultadoOperacion, respuesta: Any, error: str | None
) -> None:
    """CU-11, paso 5 y E1/E3: aplica el resultado de authenticateDocument al documento.

    - EXITO (200): AUTENTICADO, con la respuesta del centralizador guardada.
    - RECHAZO (204, o el documento se elimino -- E5): RECHAZADO, salvo que ya no exista.
    - REINTENTOS_AGOTADOS (E3): se queda en PENDIENTE, sin tocar nada mas.
    """
    if resultado == ResultadoOperacion.REINTENTOS_AGOTADOS:
        return

    documento = await session.get(Documento, uuid.UUID(payload["documento_id"]))
    if documento is None:
        return  # E5: ya no hay nada que actualizar

    if resultado == ResultadoOperacion.EXITO:
        documento.estado_autenticacion = EstadoAutenticacionDocumento.AUTENTICADO
        documento.respuesta_centralizador = str(respuesta) if respuesta is not None else None
    else:  # RECHAZO
        documento.estado_autenticacion = EstadoAutenticacionDocumento.RECHAZADO
        documento.respuesta_centralizador = error
    documento.autenticacion_actualizada_en = datetime.now(timezone.utc)


EfectoAlFinalizar = Callable[[AsyncSession, dict, ResultadoOperacion, Any, str | None], Awaitable[None]]

EFECTOS_AL_FINALIZAR: dict[str, EfectoAlFinalizar] = {
    "registerCitizen": _activar_ciudadano,
    "authenticateDocument": _actualizar_autenticacion_documento,
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
    valor = payload.get("documento_id") or payload.get("cedula")
    return str(valor) if valor is not None else None


async def _ejecutar(gov: GovCarpeta, operacion: str, payload: dict) -> tuple[Any, str | None, bool]:
    """Ejecuta la operacion. Devuelve (respuesta, error, reintentable).

    Exactamente uno de (respuesta, error) es distinto de None.
    """
    manejador = MANEJADORES.get(operacion)
    if manejador is None:
        return None, f"operacion desconocida: {operacion}", False
    try:
        respuesta = await manejador(gov, payload)
    except CentralizadorNoDisponible as exc:
        return None, str(exc)[:2000], True
    except Exception as exc:  # error de negocio (p. ej. ya registrado, documento eliminado): no se reintenta
        return None, str(exc)[:2000], False
    return respuesta, None, False


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


def _resultado_final(entrada: Outbox, reintentable: bool) -> ResultadoOperacion | None:
    """None mientras la entrada sigue PENDIENTE (se reintentara mas tarde)."""
    if entrada.estado == EstadoOutbox.COMPLETADO:
        return ResultadoOperacion.EXITO
    if entrada.estado == EstadoOutbox.FALLIDO:
        return ResultadoOperacion.REINTENTOS_AGOTADOS if reintentable else ResultadoOperacion.RECHAZO
    return None


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

        respuesta, error, reintentable = await _ejecutar(gov, operacion, payload)

        async with session_factory() as session, session.begin():
            entrada = await session.get(Outbox, entrada_id, with_for_update=True)
            assert entrada is not None
            _finalizar(entrada, intentos_previos, error, reintentable)

            resultado = _resultado_final(entrada, reintentable)
            if resultado is not None:
                efecto = EFECTOS_AL_FINALIZAR.get(operacion)
                if efecto is not None:
                    await efecto(session, payload, resultado, respuesta, error)
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
                            "resultado": resultado.value,
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
