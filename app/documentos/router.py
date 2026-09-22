"""CU-05: carga de documentos, y las consultas basicas de la carpeta (CU-06/CU-07).

Ver docs/especificacion.md: "Flujos a implementar" (flujo 3), "Contrato de la API
propia" (documentos), "Flujos alternos y de excepcion" (CU-05, A1-A2, E1-E5),
"Parametros y limites" (cuota, tamano, tipos, paginacion) y "Seguridad y manejo de
documentos" (enlaces firmados, claves de objeto aleatorias).

A1 (firma digital) no se dispara desde este endpoint: el formulario de carga
(`archivo, titulo, tipo, entidad_emisora, fecha_emision`) no trae un campo de firma
independiente, a diferencia del documento de identidad de CU-01 (que la Registraduria
entrega junto con su propio `firma`). `firma_valida` queda en NULL para lo que se sube
aqui, listo para cuando exista un flujo que si aporte una firma que validar.

A2 (sustitucion, CU-10) es explicita, no inferida: el formulario acepta `sustituye_a` con
el id del documento temporal a reemplazar. Sin ese campo, toda carga crea un documento
nuevo, aunque coincidan titulo/tipo/entidad_emisora con uno existente -- inferir la
sustitucion por esos metadatos haria que dos documentos distintos con la misma
descripcion se pisaran entre si y se perdiera el anterior sin que el ciudadano lo pidiera.
Cuando si viene, el documento anterior NO se borra: pasa a `estado=REEMPLAZADO` (fila y
objeto del bucket se conservan, sin perder su historia) y el nuevo queda enlazado a el
por `sustituye_a_id`. Un documento REEMPLAZADO deja de listarse (CU-07) y de contar
contra la cuota, pero sigue siendo consultable por `GET /documentos/{id}` para seguir el
historial hacia atras; solo un documento ELIMINADO (CU-08) deja de ser accesible del
todo.

CU-08 (`DELETE /documentos/{id}`): borrado diferido de un documento no certificado, igual
patron que la purga de `transferencia` -- se marca `ELIMINADO` con `purgar_despues_de` y
`app.interoperabilidad.outbox._purgar_documentos` lo purga fisicamente (fila y objeto)
al cumplirse el plazo.

CU-11 (autenticacion ante GovCarpeta): `POST .../autenticacion` solo escribe en outbox y
marca el documento PENDIENTE; el enlace firmado de 15 min, la llamada al centralizador y
la actualizacion final del estado las hace `app.interoperabilidad.outbox` en segundo
plano (ver ese modulo para E1-E5). A2 ("la solicitud la origina el registro y no el
ciudadano") no tiene todavia un segundo llamador real -- CU-01 no encola la autenticacion
del documento de identidad en esta entrega -- pero el propio proceso de outbox ya actua
siempre como actor "sistema" al aplicar el resultado, sea quien sea quien encolo.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db import SessionLocal
from app.documentos.almacenamiento import FalloAlmacenamiento, eliminar_objeto, generar_clave, generar_url_descarga, subir_objeto
from app.documentos.tipos import TIPOS_PERMITIDOS, detectar_content_type
from app.errors import ErrorDeNegocio
from app.identidad.dependencias import ciudadano_actual
from app.models import (
    Auditoria,
    Ciudadano,
    Documento,
    EstadoAutenticacionDocumento,
    EstadoCiudadano,
    EstadoDocumento,
    Outbox,
)

router = APIRouter(prefix="/api/v1/documentos", tags=["documentos"])

TAMANO_PAGINA_DEFECTO = 20
TAMANO_PAGINA_MAXIMO = 100


class RespuestaDocumento(BaseModel):
    id: uuid.UUID
    titulo: str
    tipo: str
    entidad_emisora: str | None
    fecha_emision: date | None
    certificado: bool
    firma_valida: bool | None
    estado_autenticacion: EstadoAutenticacionDocumento
    estado: EstadoDocumento
    # CU-10: el documento que este reemplazo, si llego por sustitucion explicita. Se
    # expone para que la sustitucion no sea una caja negra: el ciudadano puede seguir el
    # historial hacia atras consultando ese id con GET /documentos/{id}.
    sustituye_a: uuid.UUID | None
    tamano_bytes: int
    hash_sha256: str
    creado_en: datetime


class RespuestaListaDocumentos(BaseModel):
    items: list[RespuestaDocumento]
    total: int
    page: int
    size: int


class RespuestaDescarga(BaseModel):
    url: str
    expira_en: datetime


class RespuestaAutenticacion(BaseModel):
    estado: EstadoAutenticacionDocumento
    # El contrato documentado para el POST solo muestra {"estado": "PENDIENTE"}; estos
    # dos campos son una extension aditiva (igual forma que el GET) para que A1 pueda
    # "mostrar el resultado guardado" sin cambiar de forma segun el caso.
    respuesta_centralizador: str | None
    actualizado_en: datetime | None


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _exigir_activo(ciudadano: Ciudadano) -> None:
    if ciudadano.estado != EstadoCiudadano.ACTIVO:
        raise ErrorDeNegocio("ESTADO_INVALIDO", "Tu carpeta no esta activa.")


async def resolver_sustitucion(
    session: AsyncSession, *, ciudadano_id: int, sustituye_a: uuid.UUID | None
) -> Documento | None:
    """CU-10: valida y devuelve el documento que una nueva carga sustituye, o None si
    `sustituye_a` no vino. Compartido entre la carga propia del ciudadano
    (`cargar_documento`) y el depósito de una entidad emisora
    (`app.documentos.entidades`, CU-13), que también puede certificar el reemplazo de
    un documento temporal existente."""
    if sustituye_a is None:
        return None
    anterior = await session.get(Documento, sustituye_a)
    if anterior is None or anterior.estado != EstadoDocumento.ACTIVO:
        raise ErrorDeNegocio("RECURSO_NO_ENCONTRADO", "El documento a sustituir no existe.")
    if anterior.ciudadano_id != ciudadano_id:
        raise ErrorDeNegocio("NO_AUTORIZADO", "El documento a sustituir no pertenece a esa carpeta.")
    if anterior.certificado:
        raise ErrorDeNegocio("ESTADO_INVALIDO", "No se puede sustituir un documento certificado.")
    return anterior


def _a_respuesta(d: Documento) -> RespuestaDocumento:
    return RespuestaDocumento(
        id=d.id,
        titulo=d.titulo,
        tipo=d.tipo,
        entidad_emisora=d.entidad_emisora,
        fecha_emision=d.fecha_emision.date() if d.fecha_emision else None,
        certificado=d.certificado,
        firma_valida=d.firma_valida,
        estado_autenticacion=d.estado_autenticacion,
        estado=d.estado,
        sustituye_a=d.sustituye_a_id,
        tamano_bytes=d.tamano_bytes,
        hash_sha256=d.hash_sha256,
        creado_en=d.creado_en,
    )


def _a_datetime_utc(d: date, *, fin_del_dia: bool = False) -> datetime:
    hora = time.max if fin_del_dia else time.min
    return datetime.combine(d, hora, tzinfo=timezone.utc)


@router.post("", response_model=RespuestaDocumento)
async def cargar_documento(
    response: Response,
    request: Request,
    archivo: UploadFile = File(...),
    titulo: str = Form(..., min_length=1, max_length=255),
    tipo: str = Form(..., min_length=1, max_length=100),
    entidad_emisora: str | None = Form(None, max_length=255),
    fecha_emision: date | None = Form(None),
    sustituye_a: uuid.UUID | None = Form(None),
    actual: Ciudadano = Depends(ciudadano_actual),
) -> RespuestaDocumento:
    """Carga un documento en la carpeta del ciudadano autenticado.

    Recibe el archivo (`archivo`, PDF/JPEG/PNG) junto con su título, tipo y,
    opcionalmente, la entidad emisora y la fecha de emisión. Si `sustituye_a` incluye
    el id de un documento propio no certificado, ese documento se reemplaza por el
    nuevo. Devuelve 201 con los metadatos del documento creado, o 200 si el archivo ya
    se había cargado antes (mismo contenido) y no se crea uno nuevo.

    Puede rechazar la carga con 413 si el archivo excede el tamaño máximo, 415 si el
    tipo de archivo no está permitido, 409 si la cuota de almacenamiento está agotada
    o si `sustituye_a` corresponde a un documento certificado, 404 si `sustituye_a` no
    existe, o 403 si no pertenece al ciudadano autenticado.
    """
    cfg = get_config()
    contenido = await archivo.read()

    # --- E3: tamano sobre el limite -------------------------------------------------
    if len(contenido) > cfg.tamano_maximo_archivo_bytes:
        raise ErrorDeNegocio(
            "ARCHIVO_DEMASIADO_GRANDE",
            "El archivo excede el tamano maximo permitido.",
            detalle={"limite_bytes": cfg.tamano_maximo_archivo_bytes},
        )

    # --- E2: tipo no permitido (por contenido, no por extension) --------------------
    content_type = detectar_content_type(contenido)
    if content_type is None:
        raise ErrorDeNegocio(
            "TIPO_NO_PERMITIDO",
            "El tipo de archivo no esta permitido.",
            detalle={"tipos_permitidos": list(TIPOS_PERMITIDOS)},
        )

    hash_sha256 = hashlib.sha256(contenido).hexdigest()

    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, actual.id)
        assert ciudadano is not None
        _exigir_activo(ciudadano)

        # --- E5: archivo duplicado segun hash_sha256 (solo entre lo vigente: un mismo
        # archivo vuelto a cargar despues de eliminarlo o de que fuera reemplazado crea
        # un documento nuevo, no reaparece el viejo oculto) -------------------------
        r = await session.execute(
            select(Documento).where(
                Documento.ciudadano_id == ciudadano.id,
                Documento.hash_sha256 == hash_sha256,
                Documento.estado == EstadoDocumento.ACTIVO,
            )
        )
        duplicado = r.scalar_one_or_none()
        if duplicado is not None:
            response.status_code = 200
            return _a_respuesta(duplicado)

        # --- A2/CU-10: sustituye a un documento temporal, solo si el ciudadano lo pide
        anterior = await resolver_sustitucion(session, ciudadano_id=ciudadano.id, sustituye_a=sustituye_a)

        # --- E1: cuota agotada (solo temporales activos; certificados no consumen
        # cuota, y lo reemplazado/eliminado ya no cuenta) ----------------------------
        r = await session.execute(
            select(func.coalesce(func.sum(Documento.tamano_bytes), 0))
            .select_from(Documento)
            .where(
                Documento.ciudadano_id == ciudadano.id,
                Documento.certificado.is_(False),
                Documento.estado == EstadoDocumento.ACTIVO,
            )
        )
        # SUM(bigint) en Postgres devuelve NUMERIC -> Decimal; JSONResponse usa
        # json.dumps plano y no sabe serializar Decimal, hay que volverlo int.
        usado = int(r.scalar_one())
        if anterior is not None:
            # Todavia ACTIVO en este punto (se marca REEMPLAZADO mas abajo, tras superar
            # esta validacion): la consulta de arriba ya lo conto, hay que descontarlo
            # para no cobrarle al ciudadano el espacio del documento que esta dejando ir.
            usado -= anterior.tamano_bytes
        if usado + len(contenido) > cfg.cuota_ciudadano_bytes:
            raise ErrorDeNegocio(
                "CUOTA_AGOTADA",
                "La cuota de documentos temporales esta agotada.",
                detalle={"cuota_bytes": cfg.cuota_ciudadano_bytes, "usado_bytes": usado},
            )

        # --- E4: falla del almacenamiento => nada se persiste (atomico) -------------
        clave = generar_clave(content_type)
        subir_objeto(clave=clave, contenido=contenido, content_type=content_type)

        nuevo_id = uuid.uuid4()
        nuevo = Documento(
            id=nuevo_id,
            ciudadano_id=ciudadano.id,
            titulo=titulo,
            tipo=tipo,
            entidad_emisora=entidad_emisora,
            fecha_emision=_a_datetime_utc(fecha_emision) if fecha_emision else None,
            s3_key=clave,
            content_type=content_type,
            tamano_bytes=len(contenido),
            hash_sha256=hash_sha256,
            certificado=False,
            firma_valida=None,
            sustituye_a_id=anterior.id if anterior is not None else None,
        )
        session.add(nuevo)

        # CU-10: el anterior no se borra -- pasa a REEMPLAZADO, fila y objeto se
        # conservan como historia. Deja de listarse (CU-07) y de contar contra la
        # cuota (arriba), pero sigue siendo consultable por su id.
        if anterior is not None:
            anterior.estado = EstadoDocumento.REEMPLAZADO

        session.add(
            Auditoria(
                actor=str(ciudadano.id),
                accion="documento.sustituido" if anterior is not None else "documento.cargado",
                recurso=str(nuevo_id),
                ciudadano_id=ciudadano.id,
                correlation_id=_correlation_id(request),
                detalle={"documento_anterior_id": str(anterior.id) if anterior is not None else None},
            )
        )

        try:
            await session.commit()
        except Exception:
            with contextlib.suppress(FalloAlmacenamiento):
                eliminar_objeto(clave=clave)
            raise

        await session.refresh(nuevo)

    response.status_code = 201
    return _a_respuesta(nuevo)


@router.get("", response_model=RespuestaListaDocumentos)
async def listar_documentos(
    tipo: str | None = None,
    entidad: str | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    certificado: bool | None = None,
    estado_autenticacion: EstadoAutenticacionDocumento | None = None,
    q: str | None = None,
    page: int = 1,
    size: int = TAMANO_PAGINA_DEFECTO,
    actual: Ciudadano = Depends(ciudadano_actual),
) -> RespuestaListaDocumentos:
    """Busca y clasifica los documentos del ciudadano autenticado (CU-06, CU-07).

    Acepta filtros opcionales por tipo (`tipo`), entidad emisora (`entidad`, coincidencia
    parcial), rango de fecha de emisión (`desde`/`hasta`), estado de certificación
    (`certificado`), estado de autenticación ante GovCarpeta (`estado_autenticacion`) y
    texto en el título (`q`), más paginación (`page`, `size`; tamaño de página máximo
    100). Devuelve los documentos más recientes primero, junto con el total de
    resultados que coinciden con los filtros. Solo incluye los documentos vigentes de la
    carpeta: los reemplazados por una versión más reciente o eliminados no aparecen
    aquí, aunque siguen siendo consultables por su id.
    """
    page = max(page, 1)
    size = max(1, min(size, TAMANO_PAGINA_MAXIMO))

    condiciones = [Documento.ciudadano_id == actual.id, Documento.estado == EstadoDocumento.ACTIVO]
    if tipo:
        condiciones.append(Documento.tipo == tipo)
    if entidad:
        condiciones.append(Documento.entidad_emisora.ilike(f"%{entidad}%"))
    if desde:
        condiciones.append(Documento.fecha_emision >= _a_datetime_utc(desde))
    if hasta:
        condiciones.append(Documento.fecha_emision <= _a_datetime_utc(hasta, fin_del_dia=True))
    if certificado is not None:
        condiciones.append(Documento.certificado.is_(certificado))
    if estado_autenticacion is not None:
        condiciones.append(Documento.estado_autenticacion == estado_autenticacion)
    if q:
        condiciones.append(Documento.titulo.ilike(f"%{q}%"))

    async with SessionLocal() as session:
        total = (
            await session.execute(select(func.count()).select_from(Documento).where(*condiciones))
        ).scalar_one()
        resultado = await session.execute(
            select(Documento)
            .where(*condiciones)
            .order_by(Documento.creado_en.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        items = resultado.scalars().all()

    return RespuestaListaDocumentos(items=[_a_respuesta(d) for d in items], total=total, page=page, size=size)


async def _obtener_propio(session: AsyncSession, documento_id: uuid.UUID, ciudadano_id: int) -> Documento:
    """Devuelve un documento propio siempre que siga siendo accesible: ACTIVO o
    REEMPLAZADO (CU-10 lo conserva como historia consultable). Uno ELIMINADO (CU-08) se
    trata igual que si no existiera -- "dejan de ser accesibles" (especificacion,
    "Borrado")."""
    documento = await session.get(Documento, documento_id)
    if documento is None or documento.estado == EstadoDocumento.ELIMINADO:
        raise ErrorDeNegocio("RECURSO_NO_ENCONTRADO", "El documento no existe.")
    if documento.ciudadano_id != ciudadano_id:
        raise ErrorDeNegocio("NO_AUTORIZADO", "El documento no pertenece a tu carpeta.")
    return documento


@router.get("/{documento_id}", response_model=RespuestaDocumento)
async def obtener_documento(documento_id: uuid.UUID, actual: Ciudadano = Depends(ciudadano_actual)) -> RespuestaDocumento:
    """Consulta los metadatos de un documento propio, incluido uno ya reemplazado por
    una versión más reciente (para seguir su historial con `sustituye_a`).

    Devuelve 404 si el documento no existe o fue eliminado, o 403 si no pertenece al
    ciudadano autenticado.
    """
    async with SessionLocal() as session:
        documento = await _obtener_propio(session, documento_id, actual.id)
        return _a_respuesta(documento)


@router.delete("/{documento_id}", status_code=204, response_model=None)
async def eliminar_documento(
    documento_id: uuid.UUID, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> None:
    """Elimina un documento no certificado de la carpeta del ciudadano autenticado.

    El borrado es diferido: el documento deja de listarse y de ser accesible de
    inmediato, pero el objeto se conserva en el almacenamiento hasta la purga física.
    Devuelve 404 si el documento no existe o ya fue eliminado, 403 si no pertenece al
    ciudadano autenticado, 409 si el documento está certificado, o 409 si ya fue
    reemplazado por una versión más reciente.
    """
    cfg = get_config()
    async with SessionLocal() as session:
        documento = await _obtener_propio(session, documento_id, actual.id)

        if documento.certificado:
            raise ErrorDeNegocio("DOCUMENTO_CERTIFICADO", "No se puede eliminar un documento certificado.")
        if documento.estado != EstadoDocumento.ACTIVO:
            raise ErrorDeNegocio("ESTADO_INVALIDO", "El documento ya fue reemplazado por una version mas reciente.")

        documento.estado = EstadoDocumento.ELIMINADO
        documento.purgar_despues_de = datetime.now(timezone.utc) + timedelta(days=cfg.purge_delay_days)

        session.add(
            Auditoria(
                actor=str(actual.id),
                accion="documento.eliminado",
                recurso=str(documento.id),
                ciudadano_id=actual.id,
                correlation_id=_correlation_id(request),
                detalle={"purgar_despues_de": documento.purgar_despues_de.isoformat()},
            )
        )
        await session.commit()


@router.get("/{documento_id}/descarga", response_model=RespuestaDescarga)
async def descargar_documento(
    documento_id: uuid.UUID, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> RespuestaDescarga:
    """Genera un enlace temporal para descargar un documento propio.

    Devuelve una URL firmada y la fecha en que expira; la URL no se reutiliza ni se
    almacena, y deja de funcionar una vez vencida. Devuelve 404 si el documento no
    existe, o 403 si no pertenece al ciudadano autenticado.
    """
    cfg = get_config()
    async with SessionLocal() as session:
        documento = await _obtener_propio(session, documento_id, actual.id)

        url = generar_url_descarga(clave=documento.s3_key, ttl_segundos=cfg.presigned_url_ttl_descarga)
        expira_en = datetime.now(timezone.utc) + timedelta(seconds=cfg.presigned_url_ttl_descarga)

        # "Cada generacion de un enlace firmado se registra en auditoria con el
        # documento, el destino y el momento" (Seguridad y manejo de documentos).
        session.add(
            Auditoria(
                actor=str(actual.id),
                accion="documento.enlace_generado",
                recurso=str(documento.id),
                ciudadano_id=actual.id,
                correlation_id=_correlation_id(request),
                detalle={"destino": "ciudadano", "expira_en": expira_en.isoformat()},
            )
        )
        await session.commit()

    return RespuestaDescarga(url=url, expira_en=expira_en)


def _a_respuesta_autenticacion(d: Documento) -> RespuestaAutenticacion:
    return RespuestaAutenticacion(
        estado=d.estado_autenticacion,
        respuesta_centralizador=d.respuesta_centralizador,
        actualizado_en=d.autenticacion_actualizada_en,
    )


@router.post("/{documento_id}/autenticacion", status_code=202)
async def solicitar_autenticacion(
    documento_id: uuid.UUID, response: Response, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> RespuestaAutenticacion:
    """Solicita la autenticación de un documento propio ante GovCarpeta (MinTIC).

    La operación es asíncrona: responde 202 con `estado: PENDIENTE` y el resultado se
    consulta luego con `GET` sobre esta misma ruta. Si el documento ya estaba
    autenticado, responde 200 con el resultado guardado en vez de solicitarlo de nuevo;
    si ya había una solicitud en curso, responde 202 sin duplicarla. Devuelve 404 si el
    documento no existe, 403 si no pertenece al ciudadano autenticado, o 409 si el
    documento ya no está vigente (fue reemplazado por una versión más reciente).
    """
    async with SessionLocal() as session:
        documento = await _obtener_propio(session, documento_id, actual.id)

        # No tiene sentido pedirle al centralizador que autentique un documento que el
        # propio ciudadano ya sustituyo (CU-10): _obtener_propio deja pasar un
        # REEMPLAZADO porque sigue siendo consultable para ver su historia, pero aqui
        # es una operacion nueva sobre el, no una lectura -- se rechaza sin importar si
        # ya tenia un resultado guardado de antes de ser reemplazado.
        if documento.estado != EstadoDocumento.ACTIVO:
            raise ErrorDeNegocio("ESTADO_INVALIDO", "El documento ya no esta vigente: fue reemplazado por una version mas reciente.")

        # A1: ya autenticado, no se reenvia; se muestra el resultado guardado.
        if documento.estado_autenticacion == EstadoAutenticacionDocumento.AUTENTICADO:
            response.status_code = 200
            return _a_respuesta_autenticacion(documento)

        # Idempotencia (seccion "Reglas de operacion"): ya hay una solicitud en curso,
        # no se duplica la entrada de outbox.
        if documento.estado_autenticacion == EstadoAutenticacionDocumento.PENDIENTE:
            response.status_code = 202
            return _a_respuesta_autenticacion(documento)

        documento.estado_autenticacion = EstadoAutenticacionDocumento.PENDIENTE
        documento.autenticacion_actualizada_en = datetime.now(timezone.utc)

        session.add(
            Outbox(
                operacion="authenticateDocument",
                payload={
                    "documento_id": str(documento.id),
                    "cedula": actual.id,
                    "titulo": documento.titulo,
                    "correlation_id": _correlation_id(request),
                },
            )
        )
        session.add(
            Auditoria(
                actor=str(actual.id),
                accion="documento.autenticacion_solicitada",
                recurso=str(documento.id),
                ciudadano_id=actual.id,
                correlation_id=_correlation_id(request),
                detalle={},
            )
        )
        await session.commit()

        response.status_code = 202
        return _a_respuesta_autenticacion(documento)


@router.get("/{documento_id}/autenticacion", response_model=RespuestaAutenticacion)
async def consultar_autenticacion(
    documento_id: uuid.UUID, actual: Ciudadano = Depends(ciudadano_actual)
) -> RespuestaAutenticacion:
    """Consulta el resultado de la autenticación de un documento propio ante GovCarpeta.

    El estado es `NO_SOLICITADA` si nunca se pidió, `PENDIENTE` mientras se procesa,
    y `AUTENTICADO` o `RECHAZADO` con el resultado final. Devuelve 404 si el documento
    no existe, o 403 si no pertenece al ciudadano autenticado.
    """
    async with SessionLocal() as session:
        documento = await _obtener_propio(session, documento_id, actual.id)
        return _a_respuesta_autenticacion(documento)
