"""CU-16 (recepcion) y CU-03 (envio) de transferencias de ciudadanos entre operadores.

Ver docs/especificacion.md: "Contratos", "Extensiones al enviar", "Tolerancia al
recibir", "Orden de envio", "Orden de recepcion", "Aceptacion de confirmaciones", AD-09
y AD-10.

`router` (prefijo `/api`, CU-16 y la confirmacion que recibe CU-03) vive fuera de
`/api/v1` y NO usa el sobre `{"error": {...}}` de app/errors.py: responde con lo que
define el acuerdo entre los equipos del curso, para no romper a los demas operadores
(CLAUDE.md). Por eso el cuerpo se lee a mano con `request.json()` y nunca se declara un
modelo Pydantic como parametro de FastAPI -- un 422 de validacion SI pasaria por
nuestro sobre, via el manejador global de `RequestValidationError` registrado en
app/main.py.

"La ruta responde rapido": aqui no se descarga ningun documento, ni se llama a ningun
otro operador, ni al centralizador. Todo eso -- incluida la validacion de tamano y
cantidad del paso 1 de "Orden de recepcion" -- ocurre en
`app.interoperabilidad.outbox`, para no meter un sistema externo (el operador de
origen o destino) en la ruta critica, con el mismo criterio que ya se aplica al
centralizador.

`router_propio` (prefijo `/api/v1/perfil`, CU-03) es la ruta propia que el ciudadano
usa para solicitar el traslado. Esta SI usa el sobre de error de `app/errors.py`, como
cualquier otra ruta de `/api/v1` -- es una API distinta, no el acuerdo del ecosistema.
"""

from __future__ import annotations

import logging
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import get_config
from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.identidad.dependencias import ciudadano_actual
from app.models import (
    Auditoria,
    Ciudadano,
    EstadoCiudadano,
    EstadoOutbox,
    EstadoTransferencia,
    OperadorCache,
    Outbox,
    Transferencia,
)

router = APIRouter(prefix="/api", tags=["interoperabilidad"])
router_propio = APIRouter(prefix="/api/v1/perfil", tags=["traslado"])

logger = logging.getLogger("colcarpeta.transferencias")

MARCADOR_SIN_METADATOS = "metadatos no suministrados por el operador de origen"


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _ack(detalle: dict | None = None) -> JSONResponse:
    """El acuerdo del ecosistema no documenta un cuerpo de respuesta para estas rutas
    (solo el de la solicitud): se responde 200 con un cuerpo minimo que no rompe a
    clientes que solo miren el codigo HTTP."""
    return JSONResponse(status_code=200, content={"received": True, **(detalle or {})})


def _rechazo(mensaje: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": mensaje})


def _normalizar_url_documents(valor: object) -> tuple[list[tuple[str, str]], bool]:
    """"Tolerancia al recibir": texto suelto, arreglo de textos, u objeto clave->valor.

    El segundo elemento indica si los titulos son reales (vinieron del operador de
    origen, forma objeto) o sinteticos ("Documento 1", "Documento 2", ...); solo con
    titulos reales tiene sentido emparejar `documentsMetadata` por titulo.
    """
    if isinstance(valor, str) and valor:
        return [("Documento 1", valor)], False
    if isinstance(valor, list):
        return [(f"Documento {i + 1}", str(u)) for i, u in enumerate(valor) if u], False
    if isinstance(valor, dict):
        return [(str(titulo), str(url)) for titulo, url in valor.items() if url], True
    return [], False


def _indice_metadatos_por_titulo(metadatos: list) -> dict[str, dict]:
    """`documentsMetadata` no tiene una clave documentada para emparejar, pero si algun
    elemento trae `titulo`/`title`, se usa para emparejar por titulo en vez de por
    posicion (mas robusto: no depende de que el arreglo respete el orden de
    `urlDocuments`)."""
    indice: dict[str, dict] = {}
    for meta in metadatos:
        if not isinstance(meta, dict):
            continue
        titulo = meta.get("titulo") or meta.get("title")
        if titulo:
            indice[str(titulo)] = meta
    return indice


def _normalizar_documentos(url_documents: object, documents_metadata: object) -> list[dict]:
    """Combina `urlDocuments` normalizado con `documentsMetadata`. Cuando `urlDocuments`
    llego como objeto (titulo->url, la extension de AD-09) y `documentsMetadata` trae su
    propio `titulo`/`title` por elemento, empareja por titulo; si no, empareja por
    posicion. Sin metadato para un documento, queda marcado como "metadatos no
    suministrados por el operador de origen" en vez de fallar."""
    pares, titulos_reales = _normalizar_url_documents(url_documents)
    metadatos = documents_metadata if isinstance(documents_metadata, list) else []
    indice_por_titulo = _indice_metadatos_por_titulo(metadatos) if titulos_reales else {}

    documentos = []
    for i, (titulo, url) in enumerate(pares):
        if indice_por_titulo:
            meta = indice_por_titulo.get(titulo)
        else:
            meta = metadatos[i] if i < len(metadatos) and isinstance(metadatos[i], dict) else None
        if meta is None:
            documentos.append(
                {
                    "titulo": titulo,
                    "url": url,
                    "tipo": MARCADOR_SIN_METADATOS,
                    "entidad_emisora": None,
                    "fecha_emision": None,
                    "certificado": False,
                }
            )
        else:
            documentos.append(
                {
                    "titulo": titulo,
                    "url": url,
                    "tipo": meta.get("tipo") or MARCADOR_SIN_METADATOS,
                    "entidad_emisora": meta.get("entidadEmisora") or meta.get("entidad_emisora"),
                    "fecha_emision": meta.get("fechaEmision") or meta.get("fecha_emision"),
                    "certificado": bool(meta.get("certificado", False)),
                }
            )
    return documentos


def _email_valido(valor: object) -> bool:
    return isinstance(valor, str) and "@" in valor and not valor.startswith("@") and not valor.endswith("@")


@router.post("/transferCitizen")
async def recibir_transferencia(request: Request) -> JSONResponse:
    # Nunca debe dejar escapar una excepcion: el manejador global de Exception en
    # app/main.py aplicaria el sobre {"error": {...}}, y esta ruta tiene prohibido
    # usarlo (CLAUDE.md). Cualquier fallo no previsto se registra y se responde 500
    # con un cuerpo plano en su lugar.
    try:
        return await _recibir_transferencia_impl(request)
    except Exception:
        logger.exception("fallo no previsto en POST /api/transferCitizen")
        return JSONResponse(status_code=500, content={"error": "error interno"})


async def _recibir_transferencia_impl(request: Request) -> JSONResponse:
    try:
        cuerpo = await request.json()
    except Exception:
        return _rechazo("cuerpo invalido")
    if not isinstance(cuerpo, dict):
        return _rechazo("cuerpo invalido")

    try:
        cedula = int(cuerpo.get("id"))
    except (TypeError, ValueError):
        return _rechazo("id invalido")

    confirm_api = cuerpo.get("confirmAPI")
    if not confirm_api or not isinstance(confirm_api, str):
        return _rechazo("confirmAPI requerido")

    correlation_id = _correlation_id(request)

    async with SessionLocal() as session:
        # Idempotencia ("Reglas de operacion"): "una transferencia ya recibida no se
        # procesa dos veces". Si ya existe el ciudadano, o ya hay una recepcion en
        # curso para esa cedula, se descarta sin duplicar nada.
        #
        # Excepcion: TRASLADADO no cuenta como "ya existe" para este chequeo. Esa fila
        # es un ciudadano que YA SE FUE de ColCarpeta y esta pendiente de la purga
        # diferida (PURGE_DELAY_DAYS) -- si vuelve por una transferencia real antes de
        # que se cumpla, no es un duplicado de nada, es un regreso legitimo. Dejarlo
        # pasar aqui es necesario para que `_recibir_transferencia` (bandeja de salida)
        # llegue a correr y reemplace el registro viejo -- ver CLAUDE.md.
        ciudadano = await session.get(Ciudadano, cedula)
        cuenta_como_existente = ciudadano is not None and ciudadano.estado != EstadoCiudadano.TRASLADADO
        ya_en_curso = False
        if not cuenta_como_existente:
            resultado = await session.execute(
                select(Outbox.id).where(
                    Outbox.operacion == "receiveTransferCitizen",
                    Outbox.estado.in_((EstadoOutbox.PENDIENTE, EstadoOutbox.EN_PROCESO)),
                    Outbox.payload["cedula"].astext == str(cedula),
                )
            )
            ya_en_curso = resultado.first() is not None

        if cuenta_como_existente or ya_en_curso:
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="transferencia.duplicada_descartada",
                    recurso=str(cedula),
                    ciudadano_id=cedula if ciudadano is not None else None,
                    correlation_id=correlation_id,
                    detalle={"ya_en_curso": ya_en_curso},
                )
            )
            await session.commit()
            return _ack()

        documentos = _normalizar_documentos(cuerpo.get("urlDocuments"), cuerpo.get("documentsMetadata"))

        citizen_email = cuerpo.get("citizenEmail")
        payload = {
            "cedula": cedula,
            "nombre": cuerpo.get("citizenName") or f"Ciudadano {cedula}",
            "citizen_email": citizen_email if _email_valido(citizen_email) else None,
            "contact_email": cuerpo.get("contactEmail") or None,
            "confirm_api": confirm_api,
            "documentos": documentos,
            "correlation_id": correlation_id,
        }

        session.add(Outbox(operacion="receiveTransferCitizen", payload=payload))
        session.add(
            Auditoria(
                actor="sistema",
                accion="transferencia.recibida",
                recurso=str(cedula),
                correlation_id=correlation_id,
                detalle={"documentos": len(documentos), "confirm_api": confirm_api},
            )
        )
        await session.commit()

    return _ack()


def _resolver_host(url: str) -> str | None:
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def _origen_coincide(request: Request, operador: OperadorCache) -> bool:
    """Verificacion heuristica de "controles propios" (CLAUDE.md, trampa 6: el
    ecosistema no tiene autenticacion). Compara la IP que llama contra la IP resuelta
    del host registrado en `operador_cache.transfer_api_url` para ese operador.

    Es solo informativa, no una condicion de aceptacion (ver `_confirmar_recepcion_impl`):
    varios operadores del directorio estan detras de API Gateway, Azure APIM, Cloudflare
    o ngrok, donde la IP que llama nunca coincide con la del dominio publico. Rechazar
    dejaria al ciudadano afiliado en dos operadores a la vez -- justo lo que esta
    verificacion deberia evitar. El riesgo queda acotado porque solo se acepta una
    confirmacion para una transferencia que nosotros mismos iniciamos (estado ENVIADA).
    """
    if not operador.transfer_api_url or request.client is None:
        return False
    host = _resolver_host(operador.transfer_api_url)
    if not host:
        return False
    try:
        info = socket.getaddrinfo(host, None)
    except OSError:
        return False
    ips_operador = {registro[4][0] for registro in info}
    return request.client.host in ips_operador


@router.post("/transferCitizenConfirm")
async def confirmar_recepcion(request: Request) -> JSONResponse:
    # Misma razon que en recibir_transferencia: nunca dejar escapar una excepcion hacia
    # el manejador global (aplicaria nuestro sobre de error, prohibido en esta ruta).
    try:
        return await _confirmar_recepcion_impl(request)
    except Exception:
        logger.exception("fallo no previsto en POST /api/transferCitizenConfirm")
        return JSONResponse(status_code=500, content={"error": "error interno"})


async def _confirmar_recepcion_impl(request: Request) -> JSONResponse:
    try:
        cuerpo = await request.json()
    except Exception:
        return _rechazo("cuerpo invalido")
    if not isinstance(cuerpo, dict):
        return _rechazo("cuerpo invalido")

    try:
        cedula = int(cuerpo.get("id"))
        req_status = int(cuerpo.get("req_status"))
    except (TypeError, ValueError):
        return _rechazo("id o req_status invalido")

    correlation_id = _correlation_id(request)

    async with SessionLocal() as session:
        transferencia = (
            await session.execute(
                select(Transferencia).where(
                    Transferencia.ciudadano_id == cedula, Transferencia.estado == EstadoTransferencia.ENVIADA
                )
            )
        ).scalar_one_or_none()

        if transferencia is None:
            # Cubre tanto "no hay transferencia ENVIADA" como "confirmacion repetida"
            # (una vez procesada, la transferencia ya no queda en ENVIADA).
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="transferencia.confirmacion_descartada",
                    recurso=str(cedula),
                    correlation_id=correlation_id,
                    detalle={"motivo": "no hay transferencia ENVIADA para esa cedula"},
                )
            )
            await session.commit()
            return _ack()

        # El origen no coincidiendo NO descarta la confirmacion (ver _origen_coincide):
        # se registra la discrepancia en auditoria, pero se sigue procesando. El unico
        # filtro real es que exista una transferencia ENVIADA para esa cedula (ya
        # verificado arriba) -- eso ya acota el riesgo a transferencias que nosotros
        # mismos iniciamos.
        operador = await session.get(OperadorCache, transferencia.operador_destino_id)
        if operador is None or not _origen_coincide(request, operador):
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="transferencia.origen_no_verificado",
                    recurso=str(cedula),
                    ciudadano_id=cedula,
                    correlation_id=correlation_id,
                    detalle={
                        "ip_llamada": request.client.host if request.client else None,
                        "operador_destino_id": transferencia.operador_destino_id,
                        "operador_encontrado": operador is not None,
                    },
                )
            )

        ciudadano = await session.get(Ciudadano, cedula)

        if req_status == 1:
            # "Orden de envio" paso 5: marcar CONFIRMADA y programar la purga. La purga
            # fisica (borrar ciudadano/documentos pasado purgar_despues_de) es un
            # proceso periodico que todavia no existe, fuera del alcance de CU-16.
            transferencia.estado = EstadoTransferencia.CONFIRMADA
            transferencia.confirmada_en = datetime.now(timezone.utc)
            transferencia.purgar_despues_de = datetime.now(timezone.utc) + timedelta(
                days=get_config().purge_delay_days
            )
            if ciudadano is not None and ciudadano.estado == EstadoCiudadano.EN_TRANSFERENCIA:
                ciudadano.estado = EstadoCiudadano.TRASLADADO
            accion = "transferencia.confirmada"
        else:
            # "Orden de envio" paso 6: recuperar al ciudadano. Se deja en
            # PENDIENTE_CENTRALIZADOR y se reencola registerCitizen -- reutiliza el
            # mismo manejador y efecto de CU-01, que ya sabe pasarlo a ACTIVO al 201.
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
                            "correlation_id": correlation_id,
                        },
                    )
                )
            accion = "transferencia.fallida_recuperada"

        session.add(
            Auditoria(
                actor="sistema",
                accion=accion,
                recurso=str(cedula),
                ciudadano_id=cedula,
                correlation_id=correlation_id,
                detalle={"req_status": req_status},
            )
        )
        await session.commit()

    return _ack()


class SolicitudTraslado(BaseModel):
    operador_destino_id: str = Field(min_length=1)


class RespuestaTraslado(BaseModel):
    estado: EstadoCiudadano


@router_propio.post("/traslado", response_model=RespuestaTraslado, status_code=202)
async def solicitar_traslado(
    solicitud: SolicitudTraslado,
    response: Response,
    request: Request,
    actual: Ciudadano = Depends(ciudadano_actual),
) -> RespuestaTraslado:
    """CU-03: solicitar traslado a otro operador. No exige segundo factor
    (docs/especificacion.md, "Operaciones que lo exigen"). Responde rapido: el envio de
    verdad -- enlaces firmados, `unregisterCitizen`, el `POST` al destino y marcar
    `ENVIADA` -- lo hace `app.interoperabilidad.outbox` (`enviarTransferencia`).

    El operador destino se resuelve por `_id` del directorio (CLAUDE.md, trampa 5), y
    solo contra lo que ya haya en `operador_cache` en este momento -- llamar a
    `getOperators` desde la ruta violaria "el centralizador no va en la ruta critica".
    El refresco "a demanda antes de cada envio" de verdad ocurre dentro del propio
    manejador de outbox, justo antes de enviar.
    """
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, actual.id)
        assert ciudadano is not None

        if ciudadano.estado != EstadoCiudadano.ACTIVO:
            raise ErrorDeNegocio("ESTADO_INVALIDO", "Tu carpeta no esta activa.")

        # "Solo una transferencia puede estar en estado ENVIADA por ciudadano" (Modelo
        # de datos, relacion ciudadano-transferencia).
        ya_en_curso = (
            await session.execute(
                select(Transferencia.id).where(
                    Transferencia.ciudadano_id == actual.id, Transferencia.estado == EstadoTransferencia.ENVIADA
                )
            )
        ).first()
        if ya_en_curso is not None:
            raise ErrorDeNegocio("TRASLADO_EN_CURSO", "Ya hay un traslado en curso para tu cedula.")

        operador = await session.get(OperadorCache, solicitud.operador_destino_id)
        url_valida = operador is not None and operador.transfer_api_url and (
            operador.transfer_api_url.startswith("https://") or not get_config().transferencia_exigir_https
        )
        if not url_valida:
            raise ErrorDeNegocio(
                "OPERADOR_NO_DISPONIBLE",
                "El operador destino no esta en el directorio o no publica un endpoint de transferencia seguro.",
            )

        ciudadano.estado = EstadoCiudadano.EN_TRANSFERENCIA
        session.add(
            Outbox(
                operacion="enviarTransferencia",
                payload={
                    "cedula": actual.id,
                    "operador_destino_id": solicitud.operador_destino_id,
                    "correlation_id": _correlation_id(request),
                },
            )
        )
        session.add(
            Auditoria(
                actor=str(actual.id),
                accion="transferencia.solicitada",
                recurso=str(actual.id),
                ciudadano_id=actual.id,
                correlation_id=_correlation_id(request),
                detalle={"operador_destino_id": solicitud.operador_destino_id},
            )
        )
        await session.commit()

        response.status_code = 202
        return RespuestaTraslado(estado=ciudadano.estado)
