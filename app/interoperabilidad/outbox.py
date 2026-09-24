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
`confirmarTransferencia` (CU-16, recepcion); `enviarTransferencia` (CU-03, envio);
`validarFirma` (CU-09, validacion de firma digital -- la unica que no habla con el
centralizador ni con otro operador: es una validacion puramente local, encolada solo
porque consume procesador y AD-05 exige que eso corra en segundo plano, no en la
peticion del ciudadano).

Este mismo bucle de fondo (`ejecutar_bandeja_de_salida`) tambien hace mantenimiento
periodico cada `DIRECTORIO_OPERADORES_REFRESCO_SEGUNDOS` (15 min por defecto), sin
depender de un scheduler aparte (AD-07: sin broker de mensajeria dedicado):
  - Refresca `operador_cache` desde `getOperators` ("Directorio de operadores").
  - Reconcilia transferencias `ENVIADA` mas viejas que `TRANSFER_CONFIRM_TIMEOUT` sin
    confirmacion, consultando `validateCitizen` en vez de dejarlas colgadas para
    siempre ("Aceptacion de confirmaciones").
  - Purga fisicamente las transferencias `CONFIRMADA` cuyo `purgar_despues_de` ya paso
    ("Borrado").
  - Purga fisicamente los documentos `ELIMINADO` (CU-08) cuyo `purgar_despues_de` ya
    paso. Los documentos `REEMPLAZADO` (CU-10) nunca entran aqui: se conservan como
    historia, sin fecha de purga.
Ver `_tareas_periodicas`.
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
from app.interoperabilidad.transferencias import resolver_operador_por_host
from app.models import (
    Auditoria,
    Ciudadano,
    Documento,
    EstadoAutenticacionDocumento,
    EstadoCiudadano,
    EstadoDocumento,
    EstadoOutbox,
    EstadoTransferencia,
    OperadorCache,
    OrigenCiudadano,
    Outbox,
    Transferencia,
)

logger = logging.getLogger("colcarpeta.outbox")

ESPERA_REINTENTOS_MINUTOS: tuple[int, ...] = (1, 2, 4, 8, 16)
REINTENTOS_MAXIMOS = len(ESPERA_REINTENTOS_MINUTOS)
TAMANO_LOTE = 20

Manejador = Callable[[GovCarpeta, dict], Awaitable[Any]]


class DocumentoEliminado(Exception):
    """El documento se elimino entre la solicitud y el envio: CU-11, E5
    (`authenticateDocument`) y CU-09 (`validarFirma`, mismo motivo).

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


async def _validar_firma(gov: GovCarpeta, payload: dict) -> dict | None:
    """CU-09: descarga el objeto del bucket y valida su firma digital, si trae una.

    No usa `gov` (no habla con el centralizador ni con otro operador -- la validacion
    es puramente local, ver app.documentos.firma): se mantiene el parametro para no
    complicar la firma comun de `Manejador`. Corre en segundo plano y no en la peticion
    del ciudadano porque la validacion criptografica consume procesador (AD-05).

    Devuelve None cuando no hay nada que aplicar (el archivo no es un PDF, o es un PDF
    sin firma) -- distinto de un resultado real, que siempre trae `firma_valida` con
    valor concreto (`True` o `False`, nunca None dentro del dict)."""
    from app.db import SessionLocal
    from app.documentos.almacenamiento import descargar_objeto
    from app.documentos.firma import validar_firma_pdf

    documento_id = uuid.UUID(payload["documento_id"])
    async with SessionLocal() as session:
        documento = await session.get(Documento, documento_id)
        if documento is None:
            raise DocumentoEliminado(f"el documento {documento_id} ya no existe")
        s3_key = documento.s3_key
        content_type = documento.content_type

    if content_type != "application/pdf":
        return None  # CU-09 es PAdES: solo un PDF puede traer esta firma

    contenido = descargar_objeto(clave=s3_key)
    resultado = await validar_firma_pdf(contenido)
    if resultado is None:
        return None

    return {
        "firma_valida": resultado.firma_valida,
        "firmante": resultado.firmante,
        "fecha_firma": resultado.fecha_firma.isoformat() if resultado.fecha_firma else None,
    }


def _parsear_fecha(valor: Any) -> datetime | None:
    if not valor:
        return None
    try:
        fecha = datetime.fromisoformat(str(valor))
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


async def _resolver_operador_origen(session: AsyncSession, confirm_api: str | None) -> tuple[str | None, str | None]:
    """Deduce el operador de origen de una transferencia entrante a partir del host de
    `confirmAPI`, comparado contra `operador_cache` en este mismo instante -- el
    formato de transferencia acordado entre operadores no incluye un identificador de
    origen (docs/especificacion.md, "Interoperabilidad entre operadores"). Devuelve
    `(operador_id, operador_nombre)`, resuelto una sola vez en el momento de la
    recepcion y guardado como fotografia (ver `Ciudadano.origen_operador_id`) -- nunca
    una relacion viva hacia `operador_cache`. Ambos quedan en `None` si el host no
    coincide con ningun operador conocido en este instante: no se inventa uno.

    La coincidencia de host en si vive en `app.interoperabilidad.transferencias
    .resolver_operador_por_host`, compartida con `app.admin.servicios` (pantalla de
    Transferencias): dos implementaciones de la misma pregunta ("que operador esta
    detras de esta URL") podian divergir con el tiempo y mostrar operadores distintos
    para el mismo dato -- unificadas para que eso sea imposible."""
    operadores = list((await session.execute(select(OperadorCache))).scalars().all())
    operador = resolver_operador_por_host(operadores, confirm_api)
    if operador is None:
        return None, None
    return operador.id, operador.nombre


async def _recibir_transferencia(gov: GovCarpeta, payload: dict) -> None:
    """CU-16, "Orden de recepcion": valida limites, crea el ciudadano adoptando
    `citizenEmail` (AD-10), descarga los documentos y llama a validateCitizen +
    registerCitizen. Cada paso revisa lo que ya quedo hecho en un intento anterior, para
    que un reintento retome en vez de duplicar.

    Paso 4 de "Orden de recepcion" (validar la firma digital, CU-09): paso 3 encola
    `validarFirma` por cada documento PDF, una fila de outbox distinta por cada uno --
    la validacion en si (abrir y revisar el PDF completo) corre despues, en su propio
    turno de la bandeja de salida, nunca aqui: con hasta 200 documentos por
    transferencia, hacerlo en esta misma llamada la volveria lenta y competiria por
    CPU con el resto de la recepcion. Ver `_aplicar_resultado_firma` para que pasa con
    el `certificado` que declaro el operador de origen si esa firma resulta invalida
    ("cuarentena", docs/especificacion.md).

    Paso 2 tambien rechaza (ValueError, no reintentable) si `citizenEmail` ya pertenece
    a OTRA cedula distinta ya afiliada aqui: colision real entre dos ciudadanos
    distintos, ver docs/especificacion.md "Tolerancia al recibir".
    """
    from app.config import get_config
    from app.db import SessionLocal
    from app.documentos.almacenamiento import FalloAlmacenamiento, eliminar_objeto, generar_clave, subir_objeto
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

    # Paso 1b: si esta cedula ya fue nuestra y se traslado (TRASLADADO), y la purga
    # diferida de esa salida todavia no se cumplio, su fila vieja se reemplaza antes de
    # seguir. Decision (ver CLAUDE.md para la discusion completa): AD-10 no exige
    # conservar el historial local mas alla de PURGE_DELAY_DAYS una vez que esa salida
    # ya quedo CONFIRMADA -- la fila `transferencia` (que sobrevive a la purga, ver
    # `_purgar_transferencias`) es el rastro permanente, no la fila `ciudadano`. No hay
    # motivo para bloquear el regreso hasta que se cumplan los dias, y sin este paso la
    # insercion de mas abajo chocaria contra la propia `email_carpeta` de esa fila vieja
    # (UNIQUE) -- una colision contra si mismo, no contra otra persona (esa otra
    # colision, entre dos ciudadanos distintos, sigue sin resolverse: ver CLAUDE.md).
    # Solo aplica a TRASLADADO especificamente: cualquier otro estado existente para
    # esta cedula (ACTIVO, EN_TRANSFERENCIA, PENDIENTE_VERIFICACION) es un conflicto de
    # verdad -- alguien mas afirma poder enviarnos a un ciudadano que ya es, o esta por
    # ser, nuestro por otra via -- y debe seguir sin tocarse.
    async with SessionLocal() as session:
        anterior = await session.get(Ciudadano, cedula)
        if anterior is not None and anterior.estado == EstadoCiudadano.TRASLADADO:
            documentos_viejos = (
                await session.execute(select(Documento).where(Documento.ciudadano_id == cedula))
            ).scalars().all()
            claves_viejas = [d.s3_key for d in documentos_viejos]
            for documento in documentos_viejos:
                await session.delete(documento)
            await session.delete(anterior)
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="transferencia.registro_anterior_reemplazado",
                    recurso=str(cedula),
                    ciudadano_id=None,
                    correlation_id=payload.get("correlation_id"),
                    detalle={
                        "motivo": "el ciudadano vuelve por transferencia antes de que se cumpliera "
                        "la purga diferida de su traslado anterior",
                        "documentos_descartados": len(claves_viejas),
                    },
                )
            )
            # Commit propio, en su propia transaccion: el paso 2 de abajo inserta un
            # Ciudadano nuevo con la MISMA cedula (clave primaria). Si el borrado y esa
            # insercion cayeran en el mismo flush, SQLAlchemy procesa inserts antes que
            # deletes -- violaria la clave primaria contra la fila vieja, que todavia no
            # se habria borrado de verdad. Confirmando el borrado aparte, ya no existe
            # cuando el paso 2 intenta crear la fila nueva.
            await session.commit()
            for clave in claves_viejas:
                try:
                    eliminar_objeto(clave=clave)
                except FalloAlmacenamiento:
                    logger.warning("no se pudo borrar el objeto %s del registro anterior de %s", clave, cedula)

    # Paso 2: crear el ciudadano si no existe todavia (idempotente ante reintentos; tras
    # el paso 1b, tambien cubre a quien vuelve tras un traslado previo).
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
                # Politica de colision de email_carpeta entre DOS CEDULAS DISTINTAS
                # (docs/especificacion.md, "Tolerancia al recibir"; distinto del paso 1b
                # de arriba, que resuelve el regreso del MISMO ciudadano). AD-10 fija
                # email_carpeta como identificador permanente de cada ciudadano: no se
                # le puede inventar una direccion alterna ni al que ya tenemos ni al que
                # esta llegando, asi que aceptar a los dos es imposible. Se rechaza la
                # recepcion (el que ya esta afiliado aqui no se toca) en vez de dejar
                # que la UNIQUE de la base lo tumbe con un IntegrityError sin
                # diagnostico -- mismo efecto que cualquier otro rechazo de esta
                # funcion (ValueError -> RECHAZO -> req_status=0, el origen recupera al
                # ciudadano con su propio registerCitizen).
                propietario_actual = (
                    await session.execute(
                        select(Ciudadano.id).where(Ciudadano.email_carpeta == email_carpeta, Ciudadano.id != cedula)
                    )
                ).scalar_one_or_none()
                if propietario_actual is not None:
                    raise ValueError(
                        f"citizenEmail {email_carpeta} ya pertenece a la cedula {propietario_actual} en "
                        f"ColCarpeta; no se puede recibir a {cedula} con esa direccion (colision real "
                        "entre dos ciudadanos distintos, AD-10 no permite generarle una alterna a ninguno)"
                    )
            confirm_api = payload.get("confirm_api")
            operador_origen_id, operador_origen_nombre = await _resolver_operador_origen(session, confirm_api)
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
                    origen=OrigenCiudadano.TRANSFERENCIA,
                    origen_confirm_api=confirm_api,
                    origen_operador_id=operador_origen_id,
                    origen_operador_nombre=operador_origen_nombre,
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
        documento_id = uuid.uuid4()
        async with SessionLocal() as session:
            session.add(
                Documento(
                    id=documento_id,
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
            # CU-09 en CU-16 ("cuarentena"): igual que CU-05/CU-13, solo un PDF puede
            # traer una firma que valga la pena revisar, y la validacion corre en
            # segundo plano (AD-05) -- una fila de outbox por documento, nunca todas
            # de un tiron dentro de esta misma llamada, para no retener esta transaccion
            # ni competir por CPU con el resto de la recepcion. "origen": "transferencia"
            # es lo que le dice a `_aplicar_resultado_firma` que, si la firma resulta
            # invalida, tambien hay que revocarle el `certificado` que declaro el
            # operador de origen (ver ese efecto para el porque).
            if content_type == "application/pdf":
                session.add(
                    Outbox(
                        operacion="validarFirma",
                        payload={
                            "documento_id": str(documento_id),
                            "correlation_id": payload.get("correlation_id"),
                            "origen": "transferencia",
                        },
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


async def _actualizar_cache_operadores(gov: GovCarpeta, session: AsyncSession) -> None:
    """"Directorio de operadores": upsert de `operador_cache` desde `getOperators`.
    Se resuelve siempre por `_id` (nunca por `operatorName`: el directorio trae
    nombres duplicados -- CLAUDE.md, trampa 5). El recorte de espacios en
    `transfer_api_url` ya lo hace `GovCarpeta.listar_operadores`."""
    for operador in await gov.listar_operadores():
        cache = await session.get(OperadorCache, operador.id)
        if cache is None:
            session.add(OperadorCache(id=operador.id, nombre=operador.nombre, transfer_api_url=operador.transfer_api_url))
        else:
            cache.nombre = operador.nombre
            cache.transfer_api_url = operador.transfer_api_url


async def _refrescar_operador(gov: GovCarpeta, operador_id: str) -> OperadorCache | None:
    """Refresco "a demanda antes de cada envio": no hay endpoint de un solo operador en
    `getOperators`, asi que trae el directorio completo y actualiza todo el cache, pero
    solo devuelve la entrada pedida -- mas fresca que la del refresco periodico de
    `_tareas_periodicas`, para el caso en que la URL del destino cambio hace poco."""
    from app.db import SessionLocal

    async with SessionLocal() as session, session.begin():
        await _actualizar_cache_operadores(gov, session)
        return await session.get(OperadorCache, operador_id)


async def _enviar_transferencia(gov: GovCarpeta, payload: dict) -> None:
    """CU-03, "Orden de envio": genera enlaces de 24 horas, invoca `unregisterCitizen`,
    envia `POST /api/transferCitizen` al operador destino y marca la transferencia
    `ENVIADA`. Cada paso revisa lo que ya quedo hecho en un intento anterior, igual que
    `_recibir_transferencia`.
    """
    from app.config import get_config
    from app.db import SessionLocal
    from app.documentos.almacenamiento import generar_url_descarga
    from app.interoperabilidad.operadores import enviar_ciudadano
    from app.interoperabilidad.traslado_servicios import url_transferencia_utilizable

    cfg = get_config()
    cedula = payload["cedula"]
    operador_destino_id = payload["operador_destino_id"]

    async with SessionLocal() as session:
        # Idempotencia: si un intento anterior ya llego hasta el paso 4, no reenviar --
        # solo esperar la confirmacion, que llega por /api/transferCitizenConfirm (o la
        # resuelve la reconciliacion si nunca llega).
        ya_enviada = (
            await session.execute(
                select(Transferencia.id).where(
                    Transferencia.ciudadano_id == cedula, Transferencia.estado == EstadoTransferencia.ENVIADA
                )
            )
        ).first()
        if ya_enviada is not None:
            return

        ciudadano = await session.get(Ciudadano, cedula)
        if ciudadano is None:
            raise ValueError(f"el ciudadano {cedula} ya no existe")
        documentos = (
            await session.execute(select(Documento).where(Documento.ciudadano_id == cedula))
        ).scalars().all()
        nombre = ciudadano.nombre
        citizen_email = ciudadano.email_carpeta
        contact_email = ciudadano.email_personal

    operador = await _refrescar_operador(gov, operador_destino_id)
    url_valida = operador is not None and url_transferencia_utilizable(
        operador.transfer_api_url, exigir_https=cfg.transferencia_exigir_https
    )
    if not url_valida:
        # No es transitorio: reintentar no va a cambiar lo que publica el directorio en
        # los proximos segundos. No se implementa aqui la entrega por correo que
        # menciona "Directorio de operadores" para este caso -- fuera de alcance de
        # esta entrega (ver CLAUDE.md, Pendiente).
        raise ValueError(
            f"el operador destino {operador_destino_id} no esta en el directorio o "
            "no publica un transferAPIURL https"
        )

    # Paso 1: enlaces firmados de 24 horas para todos los documentos, generados de
    # nuevo en cada intento (igual razon que CU-11: nunca pueden vencer "antes de la
    # descarga" del destino porque no se reutiliza uno viejo).
    url_documents = {
        d.titulo: generar_url_descarga(clave=d.s3_key, ttl_segundos=cfg.presigned_url_ttl_transfer) for d in documentos
    }
    documents_metadata = [
        {
            "titulo": d.titulo,
            "tipo": d.tipo,
            "entidadEmisora": d.entidad_emisora,
            "fechaEmision": d.fecha_emision.date().isoformat() if d.fecha_emision else None,
            "certificado": d.certificado,
        }
        for d in documentos
    ]

    # Paso 2: unregisterCitizen. OJO -- no se verifico contra el servicio real si un
    # segundo intento sobre una cedula ya desligada se comporta de forma idempotente
    # (CLAUDE.md prohibe probar esto con cedulas inventadas); si el centralizador lo
    # rechaza de forma no transitoria, `GovCarpeta.desligar_ciudadano` lo traduce hoy en
    # `CentralizadorNoDisponible` generico (reintentable), no en un `ValueError` de
    # negocio -- puede reintentar sin llegar a buen puerto hasta agotar los 5 intentos.
    await gov.desligar_ciudadano(cedula)

    # Paso 3: POST /api/transferCitizen al destino.
    confirm_api = f"{cfg.public_base_url.rstrip('/')}/api/transferCitizenConfirm"
    await enviar_ciudadano(
        url=operador.transfer_api_url,
        cedula=cedula,
        nombre=nombre,
        citizen_email=citizen_email,
        contact_email=contact_email or "",
        url_documents=url_documents,
        documents_metadata=documents_metadata,
        confirm_api=confirm_api,
    )

    # Paso 4: marcar ENVIADA.
    async with SessionLocal() as session:
        session.add(
            Transferencia(
                ciudadano_id=cedula,
                operador_destino_id=operador_destino_id,
                confirm_api=confirm_api,
                estado=EstadoTransferencia.ENVIADA,
            )
        )
        session.add(
            Auditoria(
                actor="sistema",
                accion="transferencia.enviada",
                recurso=str(cedula),
                ciudadano_id=cedula,
                correlation_id=payload.get("correlation_id"),
                detalle={"operador_destino_id": operador_destino_id, "documentos": len(documentos)},
            )
        )
        await session.commit()


MANEJADORES: dict[str, Manejador] = {
    "registerCitizen": _registrar_ciudadano,
    "unregisterCitizen": _desligar_ciudadano,
    "authenticateDocument": _autenticar_documento,
    "receiveTransferCitizen": _recibir_transferencia,
    "confirmarTransferencia": _confirmar_transferencia_operador,
    "enviarTransferencia": _enviar_transferencia,
    "validarFirma": _validar_firma,
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

    Este mismo manejador se reutiliza para reencolar `registerCitizen` al recuperar un
    ciudadano tras un envio o una recepcion fallidos (CU-03/CU-16) -- no solo para el
    registro original. Paso 9 de CU-01 ("el sistema notifica al correo personal") solo
    aplica al registro real: `payload["notificar_registro"]` lo marca `registro.py` al
    encolar, y esos otros reencolados simplemente no lo incluyen.
    """
    if resultado != ResultadoOperacion.EXITO:
        return
    ciudadano = await session.get(Ciudadano, payload["cedula"])
    if ciudadano is not None and ciudadano.estado == EstadoCiudadano.PENDIENTE_CENTRALIZADOR:
        ciudadano.estado = EstadoCiudadano.ACTIVO
        if payload.get("notificar_registro"):
            from app.notificaciones.correo import enviar_correo

            await enviar_correo(
                session,
                ciudadano_id=ciudadano.id,
                destinatario=ciudadano.email_personal,
                asunto="Tu carpeta en ColCarpeta está activa",
                cuerpo=(
                    f"Hola {ciudadano.nombre},\n\n"
                    "Tu registro en ColCarpeta se completó. Tu dirección de carpeta es "
                    f"{ciudadano.email_carpeta}; úsala (o tu cédula) para iniciar sesión.\n"
                ),
            )
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="registro.notificacion_enviada",
                    recurso=str(ciudadano.id),
                    ciudadano_id=ciudadano.id,
                    correlation_id=payload.get("correlation_id"),
                    detalle={"enviado_a": ciudadano.email_personal},
                )
            )


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


async def _aplicar_resultado_firma(
    session: AsyncSession, payload: dict, resultado: ResultadoOperacion, respuesta: Any, error: str | None
) -> None:
    """CU-09: aplica el resultado de `_validar_firma` al documento.

    Una firma invalida NO es un rechazo: `_validar_firma` nunca levanta una excepcion
    de negocio por eso, asi que este efecto solo corre con `resultado == EXITO`. Si el
    documento no traia firma (o dejo de existir mientras tanto), `respuesta` es `None`
    y no hay nada que aplicar -- `firma_valida` se queda como estaba (`NULL`, "sin
    firma"), nunca se fuerza a `False`.

    CU-16 ("cuarentena", ver docs/especificacion.md): si el documento llego por
    transferencia (`payload["origen"] == "transferencia"`) marcado `certificado=true`
    por el operador de origen -- una afirmacion de un tercero sin autenticar, CLAUDE.md
    "trampa 6" -- y la firma resulta invalida, se le retira el `certificado`: alguien
    afirmo algo que la propia firma contradice, y esa afirmacion no tiene detras un
    canal autenticado con nosotros (a diferencia de CU-13, donde `certificado` lo
    otorga la entidad emisora autenticada directamente, y una firma ausente o invalida
    no lo toca). Si no trae firma en absoluto (`firma_valida` sigue en NULL), el
    `certificado` declarado no se toca -- CU-13 ya acepta esa misma combinacion como
    normal, y aqui no hay motivo para tratarla distinto.

    Esa revocacion cambia dos cosas para el ciudadano sin que el haya hecho nada: el
    documento empieza a contar contra su cuota de almacenamiento (antes no, por
    certificado) y se vuelve borrable (CU-08 no admite borrar documentos certificados).
    Por eso se notifica por el centro de CU-17 -- no basta con dejarlo en `auditoria`,
    que el ciudadano no puede consultar. Sobre el efecto en la cuota: no hay ninguna
    reconciliacion retroactiva ni aviso aparte de "quedaste sobre el limite" -- la cuota
    siempre se valida solo al cargar o sustituir un documento (CU-05/CU-10), nunca de
    forma continua, asi que si esto empuja al ciudadano por encima de su cuota no pasa
    nada hasta que intente cargar o sustituir algo nuevo, momento en el que esa carga
    se rechaza igual que a cualquiera que ya estuviera al limite (documentado en
    docs/especificacion.md, "Parametros y limites"). Es una decision explicita, no un
    descuido: este mismo documento ya podia haber llegado por CU-16 sin certificar
    desde un principio y nunca respeto un limite de cuota individual al recibirse (esa
    cuota solo rige la carga propia del ciudadano), asi que la revocacion no introduce
    un caso nuevo, se suma a uno que ya existia.
    """
    if resultado != ResultadoOperacion.EXITO or respuesta is None:
        return

    documento = await session.get(Documento, uuid.UUID(payload["documento_id"]))
    if documento is None:
        return

    documento.firma_valida = respuesta["firma_valida"]
    documento.firma_firmante = respuesta["firmante"]
    documento.firma_fecha = _parsear_fecha(respuesta["fecha_firma"])

    session.add(
        Auditoria(
            actor="sistema",
            accion="documento.firma_validada",
            recurso=str(documento.id),
            ciudadano_id=documento.ciudadano_id,
            correlation_id=payload.get("correlation_id"),
            detalle={
                "firma_valida": respuesta["firma_valida"],
                "firmante": respuesta["firmante"],
                "fecha_firma": respuesta["fecha_firma"],
            },
        )
    )

    if payload.get("origen") == "transferencia" and documento.certificado and respuesta["firma_valida"] is False:
        documento.certificado = False
        session.add(
            Auditoria(
                actor="sistema",
                accion="documento.certificacion_revocada_por_firma_invalida",
                recurso=str(documento.id),
                ciudadano_id=documento.ciudadano_id,
                correlation_id=payload.get("correlation_id"),
                detalle={
                    "motivo": "el operador de origen marco el documento como certificado, "
                    "pero la firma que trae no es valida",
                    "firmante": respuesta["firmante"],
                },
            )
        )

        ciudadano = await session.get(Ciudadano, documento.ciudadano_id)
        if ciudadano is not None:
            from app.notificaciones.correo import enviar_correo

            await enviar_correo(
                session,
                ciudadano_id=ciudadano.id,
                destinatario=ciudadano.email_personal,
                asunto="Un documento de tu carpeta dejó de estar certificado",
                cuerpo=(
                    f"Hola {ciudadano.nombre},\n\n"
                    f'El documento "{documento.titulo}" llegó a tu carpeta marcado como certificado, '
                    "pero no pudimos comprobar la firma digital que traía: parece que el archivo "
                    "cambió después de haberse firmado, así que ya no podemos confirmar que sea el "
                    "original.\n\n"
                    "Por eso ese documento ya no cuenta como certificado en tu carpeta. Ahora ocupa "
                    "espacio de tu cuota de almacenamiento, y puedes eliminarlo si quieres.\n\n"
                    "Si crees que esto es un error, puedes volver a solicitar el documento a quien "
                    "te lo envió.\n"
                ),
            )


async def _emitir_token_primer_acceso(session: AsyncSession, ciudadano: Ciudadano, payload: dict) -> None:
    """Un ciudadano recibido por transferencia llega sin `password_hash`: no puede
    iniciar sesion hasta establecer una contrasena. Si trajo `contactEmail`
    (`email_personal`), se genera un token de un solo uso y se envia (simulado) para
    que pueda hacerlo; si no trajo, no hay a donde enviarlo y queda pendiente sin
    inventar un canal alterno -- solo auditado.
    """
    from app.config import get_config
    from app.identidad.token_acceso import generar_token
    from app.notificaciones.correo import enviar_correo

    if not ciudadano.email_personal:
        session.add(
            Auditoria(
                actor="sistema",
                accion="primer_acceso.sin_canal",
                recurso=str(ciudadano.id),
                ciudadano_id=ciudadano.id,
                correlation_id=payload.get("correlation_id"),
                detalle={"motivo": "la transferencia no trajo contactEmail; el ciudadano queda pendiente de primer acceso"},
            )
        )
        return

    cfg = get_config()
    token, token_hash = generar_token()
    vence_en = datetime.now(timezone.utc) + timedelta(hours=cfg.primer_acceso_token_ttl_horas)
    ciudadano.token_primer_acceso_hash = token_hash
    ciudadano.token_primer_acceso_vence_en = vence_en

    await enviar_correo(
        session,
        ciudadano_id=ciudadano.id,
        destinatario=ciudadano.email_personal,
        asunto="Establece la contraseña de tu carpeta en ColCarpeta",
        cuerpo=(
            f"Hola {ciudadano.nombre},\n\n"
            "Tu carpeta se trasladó a ColCarpeta. Para poder iniciar sesión, establece tu "
            f"contraseña con este código de un solo uso (válido por {cfg.primer_acceso_token_ttl_horas} horas):\n\n"
            f"{token}\n"
        ),
    )
    session.add(
        Auditoria(
            actor="sistema",
            accion="primer_acceso.token_emitido",
            recurso=str(ciudadano.id),
            ciudadano_id=ciudadano.id,
            correlation_id=payload.get("correlation_id"),
            # El token en claro NUNCA se audita: solo la constancia de que se emitio y a
            # donde se envio.
            detalle={"enviado_a": ciudadano.email_personal, "vence_en": vence_en.isoformat()},
        )
    )


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
        if ciudadano is not None and ciudadano.password_hash is None:
            await _emitir_token_primer_acceso(session, ciudadano, payload)
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


async def _al_finalizar_envio_transferencia(
    session: AsyncSession, payload: dict, resultado: ResultadoOperacion, respuesta: Any, error: str | None
) -> None:
    """CU-03: si el envio no se completo (limites del destino, red agotada tras los 5
    reintentos, directorio sin URL utilizable, etc.), el ciudadano no puede quedar
    EN_TRANSFERENCIA para siempre. Se recupera exactamente como un `req_status = 0` real
    en `_confirmar_recepcion_impl`: vuelve a PENDIENTE_CENTRALIZADOR y se reencola
    `registerCitizen`, que reutiliza el mismo efecto de CU-01 para llegar a ACTIVO.

    Si el fallo ocurrio ANTES del paso 2 (unregisterCitizen), ese registerCitizen va a
    encontrar al ciudadano todavia afiliado y el centralizador respondera 501 "ya
    registrado" -- un rechazo de negocio, no un exito, asi que `_activar_ciudadano` no lo
    reactiva por si solo hoy. Es la misma limitacion ya aceptada en el camino de
    recuperacion de `_confirmar_recepcion_impl`, no una nueva.
    """
    if resultado == ResultadoOperacion.EXITO:
        return  # el propio manejador ya creo la fila Transferencia(ENVIADA)

    cedula = payload["cedula"]
    ciudadano = await session.get(Ciudadano, cedula)
    if ciudadano is not None and ciudadano.estado == EstadoCiudadano.EN_TRANSFERENCIA:
        ciudadano.estado = EstadoCiudadano.PENDIENTE_CENTRALIZADOR
        session.add(
            Outbox(
                operacion="registerCitizen",
                payload={
                    "cedula": cedula,
                    "nombre": ciudadano.nombre,
                    "direccion": ciudadano.direccion,
                    "email": ciudadano.email_carpeta,
                    "correlation_id": payload.get("correlation_id"),
                },
            )
        )


EfectoAlFinalizar = Callable[[AsyncSession, dict, ResultadoOperacion, Any, str | None], Awaitable[None]]

EFECTOS_AL_FINALIZAR: dict[str, EfectoAlFinalizar] = {
    "registerCitizen": _activar_ciudadano,
    "authenticateDocument": _actualizar_autenticacion_documento,
    "receiveTransferCitizen": _al_finalizar_recepcion_transferencia,
    "enviarTransferencia": _al_finalizar_envio_transferencia,
    "validarFirma": _aplicar_resultado_firma,
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


async def _reconciliar_transferencias(gov: GovCarpeta, session_factory: async_sessionmaker[AsyncSession]) -> int:
    """"Aceptacion de confirmaciones": si transcurre TRANSFER_CONFIRM_TIMEOUT sin que
    llegue `confirmAPI` para una transferencia `ENVIADA`, se consulta `validateCitizen`
    en vez de dejarla colgada para siempre.

    La pregunta que responde es una sola: ¿el ciudadano sigue siendo nuestro?

    - `204` (disponible: nadie lo afilio) -> ya no es de nadie: `FALLIDA`, se recupera
      con `registerCitizen` (mismo desenlace que `req_status = 0`).
    - `200` y el mensaje nos nombra a nosotros mismos (`OPERATOR_NAME`) -> el
      `unregisterCitizen` del envio original no surtio efecto: sigue siendo tan nuestro
      como si nunca se hubiera afiliado a nadie. Mismo desenlace que el caso anterior:
      `FALLIDA`, se recupera. Antes de esto, este caso caia en el mismo bucket que
      "afiliado a un tercero" y se quedaba colgado sin resolverse nunca.
    - `200` y el mensaje nombra a cualquier otro operador, sea o no el destino que
      elegimos -> ya no es nuestro: `CONFIRMADA` y purga programada, igual que
      `req_status = 1`. No hace falta que el nombre coincida con el destino exacto: la
      pregunta no es "¿llego a donde lo mandamos?", es "¿sigue siendo nuestro?", y si
      el centralizador dice que no, no lo es, sin importar en que operador haya
      quedado.

    Ya no queda un tercer desenlace ambiguo: los dos casos de arriba cubren cualquier
    respuesta de `validateCitizen`, así que ninguna transferencia vencida se queda sin
    resolver en esta pasada (salvo que el centralizador mismo no responda, ver abajo).
    """
    from app.config import get_config
    from app.db import SessionLocal

    cfg = get_config()
    limite = datetime.now(timezone.utc) - timedelta(seconds=cfg.transfer_confirm_timeout)
    nombre_propio = cfg.operator_name.strip().lower()
    resueltas = 0

    async with session_factory() as session:
        vencidas = (
            await session.execute(
                select(Transferencia.id, Transferencia.ciudadano_id).where(
                    Transferencia.estado == EstadoTransferencia.ENVIADA, Transferencia.enviada_en < limite
                )
            )
        ).all()

    for transferencia_id, cedula in vencidas:
        try:
            resultado = await gov.validar_ciudadano(cedula)
        except CentralizadorNoDisponible as exc:
            logger.warning("reconciliacion de transferencia %s: centralizador no disponible: %s", transferencia_id, exc)
            continue

        async with SessionLocal() as session, session.begin():
            transferencia = await session.get(Transferencia, transferencia_id, with_for_update=True)
            if transferencia is None or transferencia.estado != EstadoTransferencia.ENVIADA:
                continue  # se resolvio por otra via (confirmAPI) mientras se consultaba

            ciudadano = await session.get(Ciudadano, cedula)
            mensaje = (resultado.mensaje or "").lower()
            sigue_siendo_nuestro = resultado.disponible or (nombre_propio and nombre_propio in mensaje)

            if sigue_siendo_nuestro:
                transferencia.estado = EstadoTransferencia.FALLIDA
                if ciudadano is not None and ciudadano.estado == EstadoCiudadano.EN_TRANSFERENCIA:
                    ciudadano.estado = EstadoCiudadano.PENDIENTE_CENTRALIZADOR
                    session.add(
                        Outbox(
                            operacion="registerCitizen",
                            payload={
                                "cedula": cedula,
                                "nombre": ciudadano.nombre,
                                "direccion": ciudadano.direccion,
                                "email": ciudadano.email_carpeta,
                                "correlation_id": None,
                            },
                        )
                    )
                accion = "transferencia.reconciliada_recuperada"
            else:
                transferencia.estado = EstadoTransferencia.CONFIRMADA
                transferencia.confirmada_en = datetime.now(timezone.utc)
                transferencia.purgar_despues_de = datetime.now(timezone.utc) + timedelta(days=cfg.purge_delay_days)
                if ciudadano is not None and ciudadano.estado == EstadoCiudadano.EN_TRANSFERENCIA:
                    ciudadano.estado = EstadoCiudadano.TRASLADADO
                accion = "transferencia.reconciliada_confirmada"

            session.add(
                Auditoria(
                    actor="sistema",
                    accion=accion,
                    recurso=str(cedula),
                    ciudadano_id=cedula,
                    correlation_id=None,
                    detalle={"transferencia_id": transferencia_id, "mensaje_centralizador": resultado.mensaje},
                )
            )
            resueltas += 1

    return resueltas


async def _purgar_transferencias(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """"Borrado": purga fisica de lo que ya cumplio `purgar_despues_de`. Borra los
    documentos y sus objetos del bucket, y el ciudadano; deja registro en auditoria. La
    fila `transferencia` no se borra -- pasa a `PURGADA` -- porque es el rastro de que
    el ciudadano existio y se traslado (RNF14, retencion de auditoria)."""
    from app.db import SessionLocal
    from app.documentos.almacenamiento import FalloAlmacenamiento, eliminar_objeto

    ahora = datetime.now(timezone.utc)
    purgadas = 0

    async with session_factory() as session:
        vencidas = (
            await session.execute(
                select(Transferencia.id).where(
                    Transferencia.estado == EstadoTransferencia.CONFIRMADA,
                    Transferencia.purgar_despues_de.is_not(None),
                    Transferencia.purgar_despues_de <= ahora,
                )
            )
        ).scalars().all()

    for transferencia_id in vencidas:
        async with SessionLocal() as session, session.begin():
            transferencia = await session.get(Transferencia, transferencia_id, with_for_update=True)
            if transferencia is None or transferencia.estado != EstadoTransferencia.CONFIRMADA:
                continue  # otra pasada ya la tomo (con varias replicas corriendo)

            cedula = transferencia.ciudadano_id
            ciudadano = await session.get(Ciudadano, cedula)
            documentos = (
                await session.execute(select(Documento).where(Documento.ciudadano_id == cedula))
            ).scalars().all()
            claves = [d.s3_key for d in documentos]
            for documento in documentos:
                await session.delete(documento)
            if ciudadano is not None:
                await session.delete(ciudadano)
            transferencia.estado = EstadoTransferencia.PURGADA

            # ciudadano_id=None (no el helper de arriba): esta fila se inserta en el
            # mismo flush que borra al ciudadano, con lo que la FK ya no puede
            # apuntarle -- ver el docstring de _ciudadano_id_para_auditoria.
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="transferencia.purgada",
                    recurso=str(cedula),
                    ciudadano_id=None,
                    correlation_id=None,
                    detalle={"transferencia_id": transferencia_id, "documentos_purgados": len(claves)},
                )
            )
            await session.flush()
            for clave in claves:
                try:
                    eliminar_objeto(clave=clave)
                except FalloAlmacenamiento:
                    logger.warning(
                        "no se pudo borrar el objeto %s al purgar la transferencia %s", clave, transferencia_id
                    )
        purgadas += 1

    return purgadas


async def _purgar_documentos(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """CU-08, "Borrado": purga fisica de documentos que el ciudadano elimino y cuyo
    `purgar_despues_de` ya paso. Borra la fila y el objeto del bucket; deja registro en
    auditoria. Un documento REEMPLAZADO (CU-10) nunca tiene `purgar_despues_de` --
    nunca aparece aqui, se conserva indefinidamente como historia."""
    from app.db import SessionLocal
    from app.documentos.almacenamiento import FalloAlmacenamiento, eliminar_objeto

    ahora = datetime.now(timezone.utc)
    purgados = 0

    async with session_factory() as session:
        vencidos = (
            await session.execute(
                select(Documento.id).where(
                    Documento.estado == EstadoDocumento.ELIMINADO,
                    Documento.purgar_despues_de.is_not(None),
                    Documento.purgar_despues_de <= ahora,
                )
            )
        ).scalars().all()

    for documento_id in vencidos:
        async with SessionLocal() as session, session.begin():
            documento = await session.get(Documento, documento_id, with_for_update=True)
            if documento is None or documento.estado != EstadoDocumento.ELIMINADO:
                continue  # otra pasada ya lo tomo (con varias replicas corriendo)

            clave = documento.s3_key
            ciudadano_id = documento.ciudadano_id
            await session.delete(documento)
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="documento.purgado",
                    recurso=str(documento_id),
                    ciudadano_id=ciudadano_id,
                    correlation_id=None,
                    detalle={},
                )
            )
            await session.flush()
        try:
            eliminar_objeto(clave=clave)
        except FalloAlmacenamiento:
            logger.warning("no se pudo borrar el objeto %s al purgar el documento %s", clave, documento_id)
        purgados += 1

    return purgados


async def _tareas_periodicas(gov: GovCarpeta, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Mantenimiento periodico (ver docstring del modulo). Cada paso es independiente:
    que uno falle no debe bloquear los otros ni tumbar el bucle principal de outbox."""
    try:
        async with session_factory() as session, session.begin():
            await _actualizar_cache_operadores(gov, session)
    except CentralizadorNoDisponible as exc:
        logger.warning("no se pudo refrescar el directorio de operadores: %s", exc)

    try:
        resueltas = await _reconciliar_transferencias(gov, session_factory)
        if resueltas:
            logger.info("reconciliacion de transferencias: %s resuelta(s)", resueltas)
    except Exception:
        logger.exception("fallo reconciliando transferencias vencidas")

    try:
        purgadas = await _purgar_transferencias(session_factory)
        if purgadas:
            logger.info("purga de transferencias: %s purgada(s)", purgadas)
    except Exception:
        logger.exception("fallo purgando transferencias confirmadas")

    try:
        purgados_docs = await _purgar_documentos(session_factory)
        if purgados_docs:
            logger.info("purga de documentos: %s purgado(s)", purgados_docs)
    except Exception:
        logger.exception("fallo purgando documentos eliminados")


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
    cfg = get_config()
    intervalo = intervalo_segundos if intervalo_segundos is not None else cfg.outbox_intervalo_segundos
    gov = GovCarpeta()
    # datetime.min fuerza el primer mantenimiento en el arranque, sin esperar los 15
    # min completos -- util tambien para pruebas con un servidor recien levantado.
    ultimo_mantenimiento = datetime.min.replace(tzinfo=timezone.utc)
    try:
        while True:
            try:
                await procesar_lote(factory, gov)
                ahora = datetime.now(timezone.utc)
                if (ahora - ultimo_mantenimiento).total_seconds() >= cfg.directorio_operadores_refresco_segundos:
                    await _tareas_periodicas(gov, factory)
                    ultimo_mantenimiento = ahora
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("fallo inesperado procesando la bandeja de salida")
            await asyncio.sleep(intervalo)
    finally:
        await gov.cerrar()
        await operadores.cerrar()
