"""Proceso de bandeja de salida (outbox).

Ver docs/especificacion.md, seccion "Reglas de operacion". `validateCitizen` es la unica
llamada sincrona dentro de una peticion del ciudadano; todo lo demas que toca al
centralizador se escribe en `outbox` dentro de la misma transaccion de negocio y lo
ejecuta este proceso en segundo plano.

Parametros (seccion "Parametros y limites"):
  - Intervalo del proceso: OUTBOX_INTERVALO_SEGUNDOS (por defecto 10 s).
  - Reintentos: 5, con espera exponencial de 1, 2, 4, 8 y 16 minutos.
  - Agotados los reintentos, la entrada queda en FALLIDO.
  - Filas colgadas en EN_PROCESO (el proceso que las tomo murio, se colgo, o hubo un
    redespliegue a mitad de ejecucion) se reviven pasados OUTBOX_EN_PROCESO_MAXIMO_SEGUNDOS
    (por defecto 900 s = 15 min): ver `_recuperar_colgadas`. Cuenta como un intento mas,
    igual que cualquier otro fallo reintentable.

Se reintenta ante 500, 501, tiempo de espera agotado y error de red -- es decir, ante
`CentralizadorNoDisponible` (el centralizador) u `OperadorNoDisponible` (otro operador,
CU-16). Cualquier otro error (por ejemplo el 501 "ya registrado" de registerCitizen, o el
204 "no autenticado" de authenticateDocument, que `GovCarpeta` traduce a `ValueError`) es
de negocio y no se reintenta: la entrada pasa a FALLIDO de una vez.

Operaciones registradas en `MANEJADORES`: `registerCitizen`, `unregisterCitizen` y
`authenticateDocument` (centralizador); `receiveTransferCitizen` y
`confirmarTransferencia` (CU-16, otros operadores).
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.interoperabilidad.govcarpeta import CentralizadorNoDisponible, GovCarpeta
from app.interoperabilidad.operadores import OperadorNoDisponible
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


def _parsear_fecha(valor: Any) -> datetime | None:
    if not valor:
        return None
    try:
        fecha = datetime.fromisoformat(str(valor))
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


async def _recibir_transferencia(gov: GovCarpeta, payload: dict) -> None:
    """CU-16, "Orden de recepcion": valida limites, crea el ciudadano adoptando
    `citizenEmail` (AD-10), descarga los documentos y llama a validateCitizen +
    registerCitizen. Cada paso revisa lo que ya quedo hecho en un intento anterior, para
    que un reintento retome en vez de duplicar.

    La validacion de firma digital (paso 4 de "Orden de recepcion") queda pendiente de
    CU-09 (requiere pyHanko): `firma_valida` se deja en NULL, igual que A1 de CU-05.
    """
    from app.config import get_config
    from app.db import SessionLocal
    from app.documentos.almacenamiento import generar_clave, subir_objeto
    from app.identidad.correo import generar_email_carpeta
    from app.interoperabilidad.operadores import descargar, obtener_tamano

    cfg = get_config()
    cedula = payload["cedula"]
    documentos_entrada = payload["documentos"]

    # Paso 1: limites, antes de descargar nada.
    if len(documentos_entrada) > cfg.transferencia_documentos_maximo:
        raise ValueError(
            f"la transferencia trae {len(documentos_entrada)} documentos, "
            f"el maximo es {cfg.transferencia_documentos_maximo}"
        )
    total_estimado = 0
    for doc in documentos_entrada:
        tamano = await obtener_tamano(doc["url"])
        if tamano is not None:
            total_estimado += tamano
    if total_estimado > cfg.transferencia_tamano_maximo_bytes:
        raise ValueError(
            f"la transferencia pesa ~{total_estimado} bytes, "
            f"el maximo es {cfg.transferencia_tamano_maximo_bytes}"
        )

    # Paso 2: crear el ciudadano si no existe todavia (idempotente ante reintentos).
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, cedula)
        if ciudadano is None:
            citizen_email = payload.get("citizen_email")
            email_generado = citizen_email is None
            if email_generado:
                email_carpeta = await generar_email_carpeta(
                    session,
                    nombre_completo=payload["nombre"],
                    anio=datetime.now(timezone.utc).year,
                    dominio=cfg.dominio_carpeta,
                )
            else:
                email_carpeta = citizen_email
            session.add(
                Ciudadano(
                    id=cedula,
                    nombre=payload["nombre"],
                    direccion=payload.get("direccion") or "",
                    email_carpeta=email_carpeta,
                    email_personal=payload.get("contact_email") or "",
                    telefono=payload.get("telefono") or "",
                    password_hash=None,
                    estado=EstadoCiudadano.PENDIENTE_CENTRALIZADOR,
                    identidad_verificada=True,
                )
            )
            if email_generado:
                session.add(
                    Auditoria(
                        actor="sistema",
                        accion="transferencia.email_generado",
                        recurso=str(cedula),
                        ciudadano_id=cedula,
                        correlation_id=payload.get("correlation_id"),
                        detalle={"motivo": "citizenEmail vacio o invalido", "email_carpeta": email_carpeta},
                    )
                )
            await session.commit()

    # Paso 3: descargar y almacenar los documentos que aun no se hayan bajado.
    async with SessionLocal() as session:
        existentes = (
            await session.execute(select(Documento).where(Documento.ciudadano_id == cedula))
        ).scalars().all()
    titulos_existentes = {d.titulo for d in existentes}
    # El total ya persistido cuenta para el limite (retomar un intento colgado no debe
    # permitir superar transferencia_tamano_maximo_bytes en conjunto).
    bytes_usados = sum(d.tamano_bytes for d in existentes)

    for doc in documentos_entrada:
        if doc["titulo"] in titulos_existentes:
            continue
        presupuesto_restante = cfg.transferencia_tamano_maximo_bytes - bytes_usados
        if presupuesto_restante <= 0:
            raise ValueError(
                f"la transferencia ya alcanzo el limite de {cfg.transferencia_tamano_maximo_bytes} bytes"
            )
        # No se confia en el Content-Length que declaro el operador de origen (paso 1,
        # via obtener_tamano): es un tercero, puede mentir o simplemente no informarlo.
        # descargar() aplica el limite de verdad, en streaming, sobre los bytes que
        # realmente van llegando.
        contenido, content_type_remoto = await descargar(doc["url"], limite_bytes=presupuesto_restante)
        bytes_usados += len(contenido)
        # Algunos servidores mandan parametros pegados al Content-Type (se vio en
        # pruebas: "application/pdf; qs=0.001"), que rompen la deteccion de extension y
        # no son un media type valido para el bucket; nos quedamos solo con el tipo.
        content_type = content_type_remoto.split(";")[0].strip() if content_type_remoto else "application/octet-stream"
        clave = generar_clave(content_type)
        subir_objeto(clave=clave, contenido=contenido, content_type=content_type)
        async with SessionLocal() as session:
            session.add(
                Documento(
                    id=uuid.uuid4(),
                    ciudadano_id=cedula,
                    titulo=doc["titulo"],
                    tipo=doc["tipo"],
                    entidad_emisora=doc["entidad_emisora"],
                    fecha_emision=_parsear_fecha(doc["fecha_emision"]),
                    s3_key=clave,
                    content_type=content_type,
                    tamano_bytes=len(contenido),
                    hash_sha256=hashlib.sha256(contenido).hexdigest(),
                    certificado=doc["certificado"],
                )
            )
            await session.commit()

    # Paso 5: validateCitizen + registerCitizen en el centralizador.
    resultado = await gov.validar_ciudadano(cedula)
    if not resultado.disponible:
        raise ValueError(f"validateCitizen dice que {cedula} ya esta afiliado: {resultado.mensaje}")

    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, cedula)
        assert ciudadano is not None
        direccion, email_carpeta = ciudadano.direccion, ciudadano.email_carpeta

    await gov.registrar_ciudadano(cedula=cedula, nombre=payload["nombre"], direccion=direccion, email=email_carpeta)


async def _confirmar_transferencia_operador(gov: GovCarpeta, payload: dict) -> None:
    """CU-16, paso 6: notifica al operador de origen el resultado de la recepcion.

    No usa `gov` (no es el centralizador, es otro operador): se mantiene el parametro
    para no complicar la firma comun de `Manejador`.
    """
    from app.interoperabilidad.operadores import confirmar_transferencia

    await confirmar_transferencia(
        url=payload["confirm_api"], cedula=payload["cedula"], req_status=payload["req_status"]
    )


MANEJADORES: dict[str, Manejador] = {
    "registerCitizen": _registrar_ciudadano,
    "unregisterCitizen": _desligar_ciudadano,
    "authenticateDocument": _autenticar_documento,
    "receiveTransferCitizen": _recibir_transferencia,
    "confirmarTransferencia": _confirmar_transferencia_operador,
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


async def _al_finalizar_recepcion_transferencia(
    session: AsyncSession, payload: dict, resultado: ResultadoOperacion, respuesta: Any, error: str | None
) -> None:
    """CU-16, paso 6: activa o descarta al ciudadano recibido y encola la confirmacion
    hacia el operador de origen -- `confirmAPI` tambien va por outbox, nunca en la ruta.

    En fallo (RECHAZO o REINTENTOS_AGOTADOS) vamos a responder `req_status = 0`: estamos
    renunciando al ciudadano, y el origen lo va a recuperar con su propio
    `registerCitizen`. Dejarlo aqui en PENDIENTE_CENTRALIZADOR (como hace CU-01 en
    E5/E6) haria que existiera en dos operadores a la vez, que el caso de estudio
    prohibe -- asi que todo lo que se alcanzo a crear de esta recepcion se descarta:
    el ciudadano, sus documentos y los objetos ya subidos al bucket.
    """
    from app.documentos.almacenamiento import FalloAlmacenamiento, eliminar_objeto

    cedula = payload["cedula"]
    ciudadano = await session.get(Ciudadano, cedula)

    if resultado == ResultadoOperacion.EXITO:
        if ciudadano is not None and ciudadano.estado == EstadoCiudadano.PENDIENTE_CENTRALIZADOR:
            ciudadano.estado = EstadoCiudadano.ACTIVO
        req_status = 1
    else:
        req_status = 0
        if ciudadano is not None:
            documentos = (
                await session.execute(select(Documento).where(Documento.ciudadano_id == cedula))
            ).scalars().all()
            claves = [d.s3_key for d in documentos]
            for documento in documentos:
                await session.delete(documento)
            await session.delete(ciudadano)
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="transferencia.recepcion_descartada",
                    recurso=str(cedula),
                    ciudadano_id=cedula,
                    correlation_id=payload.get("correlation_id"),
                    detalle={
                        "motivo": error,
                        "documentos_descartados": len(claves),
                        "razon": "req_status=0: el origen recupera al ciudadano, no puede quedar afiliado aqui tambien",
                    },
                )
            )
            # flush ya, para dejar libres las filas antes de tocar el bucket (que no
            # depende de la transaccion, pero no tiene sentido intentarlo si el borrado
            # en BD fuera a fallar).
            await session.flush()
            for clave in claves:
                try:
                    eliminar_objeto(clave=clave)
                except FalloAlmacenamiento:
                    logger.warning("no se pudo borrar el objeto %s al descartar la recepcion de %s", clave, cedula)

    confirm_api = payload.get("confirm_api")
    if confirm_api:
        session.add(
            Outbox(
                operacion="confirmarTransferencia",
                payload={
                    "cedula": cedula,
                    "confirm_api": confirm_api,
                    "req_status": req_status,
                    "correlation_id": payload.get("correlation_id"),
                },
            )
        )


EfectoAlFinalizar = Callable[[AsyncSession, dict, ResultadoOperacion, Any, str | None], Awaitable[None]]

EFECTOS_AL_FINALIZAR: dict[str, EfectoAlFinalizar] = {
    "registerCitizen": _activar_ciudadano,
    "authenticateDocument": _actualizar_autenticacion_documento,
    "receiveTransferCitizen": _al_finalizar_recepcion_transferencia,
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
        entrada.tomado_en = ahora
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
    except (CentralizadorNoDisponible, OperadorNoDisponible) as exc:
        return None, str(exc)[:2000], True
    except Exception as exc:  # error de negocio (p. ej. ya registrado, documento eliminado): no se reintenta
        return None, str(exc)[:2000], False
    return respuesta, None, False


def _finalizar(entrada: Outbox, intentos_previos: int, error: str | None, reintentable: bool) -> None:
    entrada.intentos = intentos_previos + 1
    entrada.tomado_en = None  # sale de EN_PROCESO en cualquiera de los tres casos de abajo
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


async def _ciudadano_id_para_auditoria(session: AsyncSession, cedula: Any) -> int | None:
    """El efecto de finalizacion (p. ej. el descarte de CU-16 en
    `_al_finalizar_recepcion_transferencia`) puede haber borrado al ciudadano dentro de
    la misma transaccion, con su propio flush. Apuntar una auditoria nueva a esa
    `ciudadano_id` justo despues viola la FK de inmediato: `ON DELETE SET NULL` solo
    nulifica referencias que YA existian antes del borrado, no evita que un INSERT
    posterior falle contra una fila que la transaccion ya ve como borrada. Si el
    ciudadano ya no esta, la auditoria queda sin esa referencia -- `recurso` y
    `detalle` ya llevan la cedula como texto de todas formas."""
    if cedula is None:
        return None
    return cedula if await session.get(Ciudadano, cedula) is not None else None


async def _finalizar_intento(
    session: AsyncSession,
    entrada: Outbox,
    *,
    intentos_previos: int,
    error: str | None,
    reintentable: bool,
    respuesta: Any = None,
) -> ResultadoOperacion | None:
    """Aplica el resultado de un intento (exitoso, fallido, o revivido por cuelgue en
    `_recuperar_colgadas`) sobre `entrada`, corre el efecto de finalizacion si quedo en
    un estado terminal, y deja constancia en auditoria. Devuelve el resultado si fue
    terminal, o None si sigue PENDIENTE para reintentar mas tarde.

    Punto unico usado tanto por el camino normal (`procesar_lote`) como por la
    recuperacion de colgadas, para que ambos disparen el mismo efecto de limpieza --
    p. ej. el descarte de CU-16 -- sin duplicar la logica.
    """
    payload = entrada.payload
    operacion = entrada.operacion
    entrada_id = entrada.id

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
                ciudadano_id=await _ciudadano_id_para_auditoria(session, payload.get("cedula")),
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
    return resultado


async def _recuperar_colgadas(
    session_factory: async_sessionmaker[AsyncSession], umbral_segundos: int, limite: int = TAMANO_LOTE
) -> int:
    """Revive filas EN_PROCESO cuyo `tomado_en` es mas viejo que `umbral_segundos`:
    senal de que el proceso que las tomo murio, se colgo, o hubo un redespliegue a
    mitad de ejecucion -- esto puede pasar en cualquier sistema operativo, no solo en
    el cuelgue de Windows documentado en outbox.py (ver docstring del modulo).

    Se tratan como un intento fallido reintentable mas: cuenta para el limite de
    reintentos y corre el mismo efecto de finalizacion que cualquier otro fallo (p. ej.
    el descarte de CU-16, para no dejar a un ciudadano a medio crear). No se reintenta
    indefinidamente: como cualquier otro fallo, tras agotar los reintentos pasa a
    FALLIDO. Devuelve cuantas entradas revivio en esta pasada.

    A diferencia de un fallo normal de `procesar_lote` (que solo deja registro en
    `auditoria` si el resultado es terminal), aqui se audita siempre el hecho mismo de
    revivir una entrada -- vuelva a PENDIENTE o caiga en FALLIDO -- porque una fila
    colgada es en si misma una anomalia operativa que vale la pena poder rastrear
    despues, no solo su desenlace final.

    El filtro `estado == EN_PROCESO` en la misma consulta que toma el `FOR UPDATE` es lo
    que evita pisar una entrada que un worker legitimamente lento ya termino de
    finalizar entre que esta funcion la miro y la bloqueo: si ya cambio de estado,
    sencillamente no aparece en el resultado. La otra mitad de esa proteccion esta en
    `procesar_lote`, que antes de finalizar normalmente confirma que la entrada siga
    EN_PROCESO.

    `tomado_en` NULL en una fila EN_PROCESO tambien cuenta como colgada, aunque en teoria
    no deberia ocurrir (`_reclamar_uno` siempre lo pone en el mismo commit que el
    estado): en SQL, `NULL < limite_tiempo` es NULL, nunca verdadero, asi que sin este
    caso especial una fila asi quedaria invisible para esta funcion para siempre, sin
    importar cuanto tiempo pase. Se prioriza revivir estas antes que las demas
    (`order_by` las pone primero) precisamente porque no hay forma de saber hace cuanto
    llevan asi.
    """
    limite_tiempo = datetime.now(timezone.utc) - timedelta(seconds=umbral_segundos)
    recuperadas = 0
    for _ in range(limite):
        async with session_factory() as session, session.begin():
            resultado = await session.execute(
                select(Outbox)
                .where(
                    Outbox.estado == EstadoOutbox.EN_PROCESO,
                    # tomado_en NULL nunca deberia pasar en una fila EN_PROCESO (lo pone
                    # _reclamar_uno en el mismo commit que el estado), pero si por
                    # cualquier motivo pasara, NULL < limite_tiempo es NULL en SQL --
                    # nunca verdadero -- y la fila quedaria invisible para esta funcion
                    # para siempre. Se trata como "mas vieja que cualquier umbral".
                    or_(Outbox.tomado_en.is_(None), Outbox.tomado_en < limite_tiempo),
                )
                .order_by(Outbox.tomado_en.is_(None).desc(), Outbox.tomado_en)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            entrada = resultado.scalar_one_or_none()
            if entrada is None:
                return recuperadas

            if entrada.tomado_en is None:
                edad_segundos = None
                error = (
                    "la entrada quedo en EN_PROCESO sin tomado_en (no deberia pasar; "
                    "se trata como colgada de todas formas): el proceso que la tomo no la termino"
                )
            else:
                edad_segundos = round((datetime.now(timezone.utc) - entrada.tomado_en).total_seconds())
                error = (
                    f"la entrada quedo en EN_PROCESO {edad_segundos} s "
                    f"(> {umbral_segundos} s): el proceso que la tomo no la termino"
                )
            entrada_id, operacion, payload = entrada.id, entrada.operacion, entrada.payload
            logger.warning("outbox %s (%s) revivida tras quedar colgada: %s", entrada_id, operacion, error)
            await _finalizar_intento(
                session, entrada, intentos_previos=entrada.intentos, error=error, reintentable=True
            )
            # Se audita el evento de recuperacion en si mismo -- independiente de si
            # `_finalizar_intento` ya escribio su propio registro por haber llegado a un
            # estado terminal (ver docstring de esta funcion).
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="outbox.colgada_recuperada",
                    recurso=_identificador_recurso(payload),
                    ciudadano_id=await _ciudadano_id_para_auditoria(session, payload.get("cedula")),
                    correlation_id=payload.get("correlation_id"),
                    detalle={
                        "outbox_id": entrada_id,
                        "operacion": operacion,
                        "edad_segundos": edad_segundos,
                        "umbral_segundos": umbral_segundos,
                        "estado_resultante": entrada.estado.value,
                        "intentos": entrada.intentos,
                    },
                )
            )
        recuperadas += 1
    return recuperadas


async def procesar_lote(
    session_factory: async_sessionmaker[AsyncSession], gov: GovCarpeta, limite: int = TAMANO_LOTE
) -> int:
    """Procesa hasta `limite` entradas listas para intentarse. Devuelve cuantas tramito.

    Cada entrada se reclama y se finaliza en transacciones cortas separadas: la llamada de
    red (hasta 30 s de lectura) queda fuera de la transaccion para no retener el bloqueo
    de fila mientras se espera al centralizador.

    Antes de tomar entradas nuevas, revive las que quedaron colgadas en EN_PROCESO
    (ver `_recuperar_colgadas`).
    """
    from app.config import get_config

    await _recuperar_colgadas(session_factory, get_config().outbox_en_proceso_maximo_segundos)

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
            if entrada.estado != EstadoOutbox.EN_PROCESO:
                # _recuperar_colgadas ya la reviso mientras la llamada de red estaba en
                # curso (o algo mas la toco): no pisar ese resultado ni duplicar el
                # efecto de finalizacion.
                logger.warning(
                    "outbox %s (%s) ya no estaba EN_PROCESO al terminar (%s); se omite para no duplicar efectos",
                    entrada_id,
                    operacion,
                    entrada.estado.value,
                )
                continue
            await _finalizar_intento(
                session,
                entrada,
                intentos_previos=intentos_previos,
                error=error,
                reintentable=reintentable,
                respuesta=respuesta,
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
    from app.interoperabilidad import operadores

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
        await operadores.cerrar()
